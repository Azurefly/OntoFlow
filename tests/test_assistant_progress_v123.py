"""Right side assistant progress persists, is scoped, and continues after refresh."""
from __future__ import annotations
import time
from fastapi.testclient import TestClient
from app.main import create_app
from app import brain


def wait_job(client, pid, job_id):
    for _ in range(250):
        result=client.get(f'/api/projects/{pid}/jobs/{job_id}')
        assert result.status_code==200, result.text
        state=result.json()['job']['state']
        if state not in ('queued','running'):
            return result.json()
        time.sleep(0.02)
    raise AssertionError('task took too long')


def test_summary_narrates_and_survives_reload(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    with TestClient(create_app(tmp_path/'db')) as c:
        pid=c.post('/api/projects',json={'name':'测试进度'}).json()['project']['id']
        r=c.post(f'/api/projects/{pid}/documents', files={'file':('测试.txt','实体：网关\n属性：编号'.encode(),'text/plain')})
        assert r.status_code==200, r.text
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=summary')
        assert r.status_code==202,r.text
        jid=r.json()['job']['id']
        outcome=wait_job(c,pid,jid)
        assert outcome['job']['state']=='succeeded',outcome['job']
        events=outcome['assistant_events']
        assert events and events[-1]['phase']=='complete',events
        assert events[-1]['percent']==100
        assert any('正在核对文档' in e['content'] for e in events)
        view=c.get(f'/api/projects/{pid}').json()
        assert any(e['job_id']==jid and e['percent']==100 for e in view['assistant_progress'])
        # Partial cursor only returns events newer than the supplied cursor.
        last=events[-1]['created_at']
        assert c.get(f'/api/projects/{pid}/jobs/{jid}',params={'since':last}).json()['assistant_events']==[]
        # Other projects must not see these events.
        pid2=c.post('/api/projects',json={'name':'另一个空间'}).json()['project']['id']
        assert c.get(f'/api/projects/{pid2}').json()['assistant_progress']==[]
        assert c.get(f'/api/projects/{pid2}/jobs/{jid}').status_code==404


def test_progress_throttling_and_safe_delete(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    with TestClient(create_app(tmp_path/'db')) as c:
        pid=c.post('/api/projects',json={'name':'删除进度测试'}).json()['project']['id']
        # Insert an inert completed workflow job so there is no active worker.
        from app.store import uid,now
        jid=uid()
        with c.app.state.store.tx() as conn:
            conn.execute('''INSERT INTO workflow_jobs(id,project_id,job_kind,stage,state,phase,
                completed_units,total_units,detail,error,source_fingerprint,result,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (jid,pid,'summary','','succeeded','complete',1,1,'','', 'x','{}',now(),now()))
        announce=c.app.state.announce_job_progress
        assert announce(pid,jid,'TOPIC','reading','正在读取第 1 段',percent=10,force=True)
        assert not announce(pid,jid,'TOPIC','reading','类似的进度',percent=12)
        assert announce(pid,jid,'TOPIC','reading','已经有新的处理结果',percent=20)
        assert c.app.state.store.one('SELECT COUNT(*) AS n FROM job_progress_events WHERE project_id=?',(pid,))['n']==2
        revision=c.get(f'/api/projects/{pid}').json()['project']['revision']
        r=c.request('DELETE',f'/api/projects/{pid}',json={'confirm_name':'删除进度测试','expected_revision':revision})
        assert r.status_code==200,r.text
        assert c.app.state.store.one('SELECT COUNT(*) AS n FROM job_progress_events WHERE project_id=?',(pid,))['n']==0


def test_model_extraction_progress_has_real_summary_evidence(tmp_path,monkeypatch):
    """Synthetic model callback exercises the real background progress pipeline."""
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    def synthetic_summary(documents,on_progress=None,baseline=None):
        on_progress('reading',0,2,'正在阅读《设备说明》：网关包含主板，等待模型输出')
        on_progress('summarizing',1,2,'实际摘要发现：网关和主板存在组成关系')
        on_progress('reading',1,2,'正在阅读第二部分：主板提供网口')
        on_progress('summarizing',2,2,'实际摘要发现：主板提供网口')
        return {'summary':'网关包含主板。','scope':'构建设备本体。','coverage':{}}
    monkeypatch.setattr(brain,'analyze',synthetic_summary)
    with TestClient(create_app(tmp_path/'db')) as c:
        pid=c.post('/api/projects',json={'name':'阶段事实'}).json()['project']['id']
        c.post(f'/api/projects/{pid}/documents', files={'file':('device.txt',b'anything','text/plain')})
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=summary')
        outcome=wait_job(c,pid,r.json()['job']['id'])
        assert outcome['job']['state']=='succeeded'
        events=outcome['assistant_events']
        assert any('网关和主板' in e['content'] for e in events)
        assert any(e['phase']=='reading' for e in events)
        assert events[-1]['phase']=='complete' and events[-1]['percent']==100
