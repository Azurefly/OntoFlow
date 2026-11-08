"""Journaled OpenMind definition sync. No blind POST replay, no direct remote SQL.

Every network side effect is preceded by a durable expected payload and stable
UUID. Unknown results are reconciled by UUID + full semantic hash, not CODE alone.
All local journal/mapping changes are fenced by a renewable per-workspace lock.
"""
from __future__ import annotations
import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime,timezone
from .store import uid,j,now
from .task_leases import timestamp
from .openmind import OpenMindError,parse_catalog
from .openmind_baseline import canonical_catalog,baseline_hash
from .native_semantics import prepare_write,semantic_hash,semantically_equal
from .sync_conflicts import conflict_summary

_PLURAL={'entity':'entities','attribute':'attributes','relation':'relations'}

class SyncBusy(OpenMindError):pass

class SyncSession:
    def __init__(self,store,pid,endpoint,branch,revision,actor):
        self.store=store;self.pid=pid;self.endpoint=endpoint;self.branch=branch
        self.revision=revision;self.actor=actor;self.run_id=uid();self.generation=None
        self.catalog_reads=0;self.writes=0;self.reconciled=0;self.sent=[]

    def acquire(self):
        with self.store.tx() as c:
            if self.store.is_postgres:c.execute('SELECT id FROM projects WHERE id=? FOR UPDATE',(self.pid,)).fetchone()
            else:c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM sync_locks WHERE project_id=?',(self.pid,)).fetchone()
            if row and row['lease_until']>timestamp():raise SyncBusy('同一工作空间已有同步/对账操作，禁止并发写入')
            self.generation=(row['generation']+1) if row else 1
            c.execute('''INSERT INTO sync_locks(project_id,owner,generation,lease_until) VALUES(?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,lease_until=excluded.lease_until''',
                (self.pid,self.run_id,self.generation,timestamp(120)))
            c.execute('INSERT INTO openmind_sync_runs VALUES(?,?,?,?,?,?,?)',
                      (self.run_id,self.pid,self.revision,'running','{}',now(),now()))
        return self

    @contextmanager
    def transaction(self):
        with self.store.tx() as c:
            if not self.store.is_postgres:c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM sync_locks WHERE project_id=?'+(' FOR UPDATE' if self.store.is_postgres else ''),(self.pid,)).fetchone()
            if (not row or row['owner']!=self.run_id or row['generation']!=self.generation or row['lease_until']<=timestamp()):
                raise SyncBusy('同步租约已失效，禁止旧同步进程继续提交')
            c.execute('UPDATE sync_locks SET lease_until=? WHERE project_id=?',(timestamp(120),self.pid))
            yield c

    def release(self,status,result):
        if self.generation is None:return
        try:
            with self.transaction() as c:
                c.execute('UPDATE openmind_sync_runs SET status=?,result=?,updated_at=? WHERE id=?',
                          (status,j(result),now(),self.run_id))
                c.execute('UPDATE sync_locks SET lease_until=? WHERE project_id=? AND owner=? AND generation=?',
                          (timestamp(-1),self.pid,self.run_id,self.generation))
                self.store.audit(c,self.pid,self.actor,'openmind_sync_finished',
                    {'run_id':self.run_id,'status':status,'writes':self.writes,
                     'catalog_reads':self.catalog_reads,'reconciled':self.reconciled})
        except SyncBusy:
            pass  # never overwrite a new owner's state or release its lock

    def catalog(self,client):
        with self.transaction():pass
        result=canonical_catalog(client.catalog(self.branch));self.catalog_reads+=1
        return result

    def _mapping(self,c,operation,remote):
        for lid in json.loads(operation['local_ids']):
            c.execute('''INSERT INTO openmind_mappings VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(project_id,kind,local_id) DO UPDATE SET remote_id=excluded.remote_id,
                code=excluded.code,lock_version=excluded.lock_version,updated_at=excluded.updated_at''',
                (self.pid,operation['kind'],lid,operation['remote_id'],operation['code'],remote.get('lockVersion'),now()))

    def reconcile(self,baseline,current,*,allow_external=False):
        """Return True only when observed drift is entirely our journaled writes.

        Reconciliation records actual outcomes; it never sends a request or
        treats a similar name/CODE as proof of having made a write.
        """
        expected=copy.deepcopy(canonical_catalog(baseline))
        indexed=parse_catalog(current)
        ops=self.store.many('''SELECT * FROM openmind_operations WHERE project_id=? AND endpoint_hash=? AND branch_id=?
            ORDER BY created_at,id''',(self.pid,self.endpoint,self.branch))
        latest={};conflicts=[]
        for op in ops:latest[(op['kind'],op['remote_id'])]=op
        for (kind,remote_id),op in latest.items():
            # A prior conflict can only be un-frozen by proven readback or a
            # recorded human decision revoking the old authorization.
            if op['state']=='REVIEW_REQUIRED':
                continue
            actual=indexed[kind].get(remote_id)
            intended=json.loads(op['desired']);before=json.loads(op['before_json'])
            if actual is not None and semantically_equal(kind,actual,intended):
                if str(actual.get('id'))!=remote_id:raise OpenMindError('回读对象身份不匹配')
                with self.transaction() as c:
                    self._mapping(c,op,actual)
                    c.execute("UPDATE openmind_operations SET state='CONFIRMED',error='',updated_at=? WHERE id=?",(timestamp(),op['id']))
                items=[x for x in expected[_PLURAL[kind]] if str(x['id'])!=remote_id]
                expected[_PLURAL[kind]]=items+[actual]
                if op['state']!='CONFIRMED':self.reconciled+=1
            elif op['state'] in ('PREPARED','UNKNOWN','ACKNOWLEDGED','CONFLICT'):
                not_applied=(actual is None and not before) or (actual and before and semantically_equal(kind,actual,before)
                                                               and actual.get('lockVersion')==before.get('lockVersion'))
                if not_applied:
                    with self.transaction() as c:
                        c.execute("UPDATE openmind_operations SET state='NOT_APPLIED',updated_at=? WHERE id=?",(timestamp(),op['id']))
                else:
                    with self.transaction() as c:
                        c.execute("UPDATE openmind_operations SET state='CONFLICT',error='回读内容与已审批请求及写前快照均不一致',updated_at=? WHERE id=?",(timestamp(),op['id']))
                    conflicts.append(conflict_summary(op,actual))
        if conflicts:raise OpenMindError('；'.join(conflicts[:3])+ (f'；另有 {len(conflicts)-3} 项冲突' if len(conflicts)>3 else ''))
        own_only=baseline_hash(canonical_catalog(expected))==baseline_hash(current)
        if not own_only and not allow_external:
            raise OpenMindError('OpenMind 目录发生变化，包含非本次审批操作造成的变更；请刷新基线并重新审核，禁止覆盖')
        return own_only

    def assert_revision(self):
        project=self.store.one('SELECT revision,published_version FROM projects WHERE id=?',(self.pid,))
        if not project or project['revision']!=self.revision:
            raise OpenMindError('本地审核修订已变化，停止旧同步计划，已发送操作仍按日志回读')

    def prepare(self,step,existing):
        self.assert_revision()
        desired=prepare_write(step,existing)
        request_hash=semantic_hash(step['kind'],desired)
        op_id=hashlib.sha256('|'.join((self.pid,self.endpoint,self.branch,str(self.revision),
                            step['kind'],desired['id'],request_hash)).encode()).hexdigest()
        with self.transaction() as c:
            row=c.execute('SELECT * FROM openmind_operations WHERE id=?',(op_id,)).fetchone()
            if not row:
                c.execute('''INSERT INTO openmind_operations
                    (id,project_id,run_id,endpoint_hash,branch_id,revision,kind,local_ids,
                     remote_id,code,action,desired,request_hash,before_json,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (op_id,self.pid,self.run_id,self.endpoint,self.branch,self.revision,step['kind'],
                     j(step.get('local_ids',[step['local_id']])),desired['id'],desired['code'],
                     'update' if existing else 'create',j(desired),request_hash,j(existing or {}),
                     'PREPARED',timestamp(),timestamp()))
            elif row['state'] in ('UNKNOWN','PREPARED','ACKNOWLEDGED','CONFLICT'):
                raise OpenMindError('操作尚未对账，不能再次发送：'+op_id)
            else:
                c.execute("UPDATE openmind_operations SET state='PREPARED',run_id=?,before_json=?,updated_at=? WHERE id=?",
                          (self.run_id,j(existing or {}),timestamp(),op_id))
            c.execute('UPDATE openmind_operations SET attempts=attempts+1 WHERE id=?',(op_id,))
        return op_id,desired

    def execute(self,client,plan,current):
        index=parse_catalog(current)
        for step in plan['steps']:
            self.assert_revision()
            kind=step['kind'];rid=step['data']['id'];existing=index[kind].get(rid)
            desired=prepare_write(step,existing)
            if existing and semantically_equal(kind,existing,desired):
                with self.transaction() as c:
                    self._mapping(c,{'kind':kind,'remote_id':rid,'code':desired['code'],
                                  'local_ids':j(step.get('local_ids',[step['local_id']]))},existing)
                self.sent.append({'kind':kind,'remote_id':rid,'reused':True})
                continue
            op_id,desired=self.prepare(step,existing)
            try:
                self.writes+=1
                response=client.write(self.branch,step,existing)
                if not isinstance(response,dict) or str(response.get('id'))!=rid:
                    raise OpenMindError('写响应缺少匹配 UUID，结果须先对账')
            except Exception as exc:
                with self.transaction() as c:
                    c.execute("UPDATE openmind_operations SET state='UNKNOWN',error=?,updated_at=? WHERE id=?",
                              ((str(exc) or type(exc).__name__)[:900],timestamp(),op_id))
                raise OpenMindError('写入结果未确定；已保存操作 '+op_id+'，下次先回读完整语义再恢复，禁止盲目重复写入。') from exc
            with self.transaction() as c:
                c.execute("UPDATE openmind_operations SET state='ACKNOWLEDGED',updated_at=? WHERE id=?",(timestamp(),op_id))
            index[kind][rid]={**desired,**response}
            self.sent.append({'kind':kind,'remote_id':rid,'operation_id':op_id})
        final=self.catalog(client)
        # This read confirms both writes and preserved remote-owned fields.
        self.reconcile(current,final)
        for step in plan['steps']:
            remote=parse_catalog(final)[step['kind']].get(step['data']['id'])
            expected=prepare_write(step,parse_catalog(current)[step['kind']].get(step['data']['id']))
            if not remote or not semantically_equal(step['kind'],remote,expected):
                raise OpenMindError('保存后回读语义不一致，不能宣称写入核验通过')
        return final
