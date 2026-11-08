"""Deterministically observe the FIRST terminal commit, not an eventual poll.

A v1.4.0 race let polling return succeeded before run_job's final metrics write.
This test observes the commit boundary itself; sleeping/retrying a flaky test is
not a repair. All data is synthetic and the test never calls external services.
"""
import json
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.metrics import attach_to_job_result


@pytest.mark.parametrize('task',['summary','propose'])
def test_first_terminal_commit_includes_measured_stats(tmp_path,monkeypatch,task):
    monkeypatch.setenv('STRUCTURED_EXTRACTION','1')
    with TestClient(create_app(tmp_path)) as client:
        created=client.post('/api/projects',json={'name':'提交时序回归'})
        assert created.status_code==200,created.text
        pid=created.json()['project']['id']
        upload=client.post(f'/api/projects/{pid}/documents',files={'file':('source.txt','实体：设备\n实体：自然人'.encode(),'text/plain')})
        assert upload.status_code==200,upload.text
        if task=='propose':
            assert client.put(f'/api/projects/{pid}/topic',json={'summary':'设备和自然人类型','scope':'本体类型定义'}).status_code==200
            assert client.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'设备台账','include_types':['设备']}).status_code==200
        store=client.app.state.store
        original=store.tx
        first_terminal=[]
        @contextmanager
        def observed_tx(*args,**kwargs):
            with original(*args,**kwargs) as conn:
                yield conn
                row=conn.execute("SELECT result,state FROM workflow_jobs WHERE project_id=? AND job_kind=? AND state IN ('succeeded','partial','failed') ORDER BY created_at DESC LIMIT 1",(pid,task)).fetchone()
                if row and not first_terminal and not kwargs.get('read_only',False):
                    first_terminal.append(json.loads(row['result']))
        monkeypatch.setattr(store,'tx',observed_tx)
        start=client.post(f'/api/projects/{pid}/jobs/start?kind={task}')
        assert start.status_code==202,start.text
        jid=start.json()['job']['id']
        for _ in range(300):
            state=client.get(f'/api/projects/{pid}/jobs/{jid}').json()['job']
            if state['state'] not in ('running','queued'):break
            time.sleep(.01)
        assert state['state']=='succeeded',state
        assert first_terminal and 'metrics' in first_terminal[0]
        stats=first_terminal[0]['metrics']
        assert 'elapsed_seconds' in stats
        if task=='propose':assert stats['source_cache_misses']>=1


def test_no_active_metrics_scope_does_not_fabricate_zero(tmp_path):
    # An unmeasured historical result must stay unmeasured, not be relabeled zero.
    class NoAccess:
        def execute(self,*args):raise AssertionError('no scope must not query or fabricate metrics')
    attach_to_job_result(NoAccess(),'unused')
