"""Explicit cross-page approval, frozen membership and durable per-item commits.

No candidate is approved merely because it arrives while a batch is running.
This operates only on the LOCAL draft; it never calls OpenMind or publishes.
"""
from __future__ import annotations
import hashlib
import json
from fastapi import HTTPException
from .store import j, now
from .task_leases import LostLease
from .ontology import InvalidOntology
from .metrics import attach_to_job_result

MAX_REVIEW_ALL = 10000

def item_digest(row):
    content={k:row[k] for k in ('id','kind','stage','payload','evidence')}
    return hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def make_snapshot(conn,pid,kind,stage):
    project=conn.execute('SELECT revision,proposal_generation,scope_confirmed,stage FROM projects WHERE id=?',(pid,)).fetchone()
    if not project:raise HTTPException(404,'工作空间不存在')
    if not project['scope_confirmed']:raise HTTPException(409,'请先确认本体主题，再执行全部同意')
    if project['stage']!=stage:raise HTTPException(409,'全部同意只作用于当前构建阶段，请切换到对应阶段')
    total=conn.execute("SELECT COUNT(*) AS n FROM proposals WHERE project_id=? AND kind=? AND stage=? AND status='pending'",(pid,kind,stage)).fetchone()['n']
    if total>MAX_REVIEW_ALL:raise HTTPException(422,f'单次全部同意最多 {MAX_REVIEW_ALL} 条，请分阶段处理；未做任何审核')
    rows=conn.execute("SELECT id,kind,stage,payload,evidence FROM proposals WHERE project_id=? AND kind=? AND stage=? AND status='pending' ORDER BY created_at,id",(pid,kind,stage)).fetchall()
    items=[];operations={'add':0,'update':0,'delete':0}
    for row in rows:
        payload=json.loads(row['payload'])
        operation=payload.get('operation','add')
        operations[operation]=operations.get(operation,0)+1
        label=str(payload.get('name') or payload.get('predicate') or row['id'])[:180]
        items.append({'id':row['id'],'hash':item_digest(row),'label':label})
    raw={'project_id':pid,'kind':kind,'stage':stage,'revision':project['revision'],
         'scope_confirmed':bool(project['scope_confirmed']),'items':items}
    fingerprint=hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {**raw,'fingerprint':fingerprint,'total':len(items),'operations':operations}

def run_review_all(store,pid,jid,stage,apply,announce):
    """Called within the existing worker lease scope; checkpoint is in same tx as decision."""
    initial=store.one('SELECT result FROM workflow_jobs WHERE id=? AND project_id=?',(jid,pid))
    if not initial:raise HTTPException(404,'全部审核任务不存在')
    report=json.loads(initial['result']); items=report['items']; total=len(items)
    actor=report['actor'];kind=report['kind']
    announce(pid,jid,stage,'reviewing',f'正在跨页同意本次已确认的 {total} 条候选；后续新增候选不在本次范围。',percent=int(100*report['cursor']/max(total,1)),force=True)
    for index in range(report['cursor'],total):
        entry=items[index]
        with store.tx() as c:
            if store.is_postgres:
                project=c.execute('SELECT revision,scope_confirmed,stage FROM projects WHERE id=? FOR UPDATE',(pid,)).fetchone()
            else:
                # store.tx already fences the job. BEGIN IMMEDIATE must precede
                # any non-lease writes and is safe for its SQLite transaction.
                if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
                project=c.execute('SELECT revision,scope_confirmed,stage FROM projects WHERE id=?',(pid,)).fetchone()
            # The expected revision tracks only decisions committed by THIS task.
            # Concurrent edits never silently enlarge the approval authority.
            if (not project or project['revision']!=report['revision'] or
                    not project['scope_confirmed'] or project['stage']!=stage):
                report['stopped_reason']='主题、阶段或本体修订被其他操作更改；已成功项保留，剩余项未处理，请刷新后重新确认'
                report['unprocessed']=total-index
                c.execute("UPDATE workflow_jobs SET state='partial',phase='partial',result=?,detail=?,updated_at=? WHERE id=?",
                          (j(report),report['stopped_reason'],now(),jid))
                store.record_action(c,pid,stage,'跨页全部同意因审核上下文变动而停止。',report['stopped_reason'])
                attach_to_job_result(c,jid)
                break
            c.execute('SAVEPOINT review_all_item')
            try:
                q=c.execute('SELECT * FROM proposals WHERE project_id=? AND id=?',(pid,entry['id'])).fetchone()
                if not q:raise HTTPException(404,'候选不存在，未修改')
                if q['status']!='pending':raise HTTPException(409,'已被其他操作审核，保持现状')
                if q['kind']!=kind or q['stage']!=stage or item_digest(q)!=entry['hash']:
                    raise HTTPException(409,'候选内容已改变，不在本次确认范围，保持待审')
                apply(c,pid,q,'accept',report.get('note','跨页全部同意'))
                store.revision(c,pid)
                store.audit(c,pid,actor,'proposal_all_accept_item',{'proposal_id':q['id'],'job_id':jid})
                c.execute('RELEASE SAVEPOINT review_all_item')
            except LostLease:
                raise
            except Exception as exc:
                c.execute('ROLLBACK TO SAVEPOINT review_all_item')
                c.execute('RELEASE SAVEPOINT review_all_item')
                reason=(str(exc.detail) if isinstance(exc,HTTPException) else
                        str(exc) if isinstance(exc,InvalidOntology) else
                        '校验或数据库约束失败，本项未改动；请查看服务端日志')
                report['failed']+=1
                report['failures'].append({'id':entry['id'],'name':entry['label'],
                                          'reason':reason[:500]})
                store.audit(c,pid,actor,'proposal_all_accept_failed',{'proposal_id':entry['id'],'job_id':jid,'reason':reason[:500]})
            else:
                report['succeeded']+=1
                report['revision']+=1
            report['cursor']=index+1
            report['unprocessed']=total-index-1
            detail=f'跨页审核 {index+1}/{total}：成功 {report["succeeded"]}，失败 {report["failed"]}；失败项保持原状态'
            c.execute("UPDATE workflow_jobs SET phase='reviewing',completed_units=?,detail=?,result=?,updated_at=? WHERE id=?",
                      (index+1,detail,j(report),now(),jid))
        if (index+1)%20==0:
            announce(pid,jid,stage,'reviewing',detail,percent=int(100*(index+1)/max(total,1)))
    if not report.get('stopped_reason'):
        report['unprocessed']=0
        state='partial' if report['failed'] else 'succeeded'
        detail=f'全部同意已结束：本次 {total} 条，成功 {report["succeeded"]} 条、失败 {report["failed"]} 条。只更新本地草稿，未发布、未写入 OpenMind。'
        with store.tx() as c:
            c.execute('UPDATE workflow_jobs SET state=?,phase=?,completed_units=?,result=?,detail=?,updated_at=? WHERE id=?',
                      (state,'partial' if report['failed'] else 'complete',total,j(report),detail,now(),jid))
            store.record_action(c,pid,stage,f'用户跨页同意 {total} 条{kind}候选。',detail)
            store.audit(c,pid,actor,'proposal_all_accept_finished',{'job_id':jid,'succeeded':report['succeeded'],'failed':report['failed']})
            attach_to_job_result(c,jid)
    else:
        detail=report['stopped_reason']
    announce(pid,jid,stage,'partial' if report['failed'] or report.get('unprocessed') else 'complete',detail,
             percent=int(100*report['cursor']/max(total,1)),force=True)
    return report
