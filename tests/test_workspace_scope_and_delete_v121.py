"""Regression tests for workspace-scoped remote UUIDs and safe local deletion."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import now, uid
from app.pg_store import PgStore

REMOTE='ec45cfd1-1f24-480c-bce3-daeabf388ce4'


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('DATABASE_URL',raising=False)
    for name in ('OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN','OPENMIND_USERNAME',
                 'OPENMIND_PASSWORD','OPENMIND_AUTH_MODE'):
        monkeypatch.delenv(name,raising=False)
    return TestClient(create_app(tmp_path/'workspace-data'))


def project(c, name):
    response=c.post('/api/projects',json={'name':name})
    assert response.status_code==200,response.text
    return response.json()['project']


def stub(c, pid):
    with c.app.state.store.tx() as conn:
        conn.execute('''INSERT INTO entities
            (id,project_id,name,kind,description,class_id,evidence,origin)
            VALUES(?,?,?,?,?,?,?,?)''',
            (REMOTE,pid,'网关','class','',None,'[]','openmind'))


def test_two_workspaces_share_remote_uuid_but_local_relations_are_isolated(tmp_path,monkeypatch):
    with setup(monkeypatch,tmp_path) as c:
        one=project(c,'工作空间甲');two=project(c,'工作空间乙')
        stub(c,one['id']);stub(c,two['id'])
        store=c.app.state.store
        with store.tx() as sql:
            for pid in (one['id'],two['id']):
                sql.execute('''INSERT INTO attributes
                    (id,project_id,owner_id,name,datatype,required,multivalued,description,evidence)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (uid(),pid,REMOTE,'serial_'+pid,'string',0,0,'','[]'))
                sql.execute('''INSERT INTO relations
                    (id,project_id,source_id,target_id,predicate,description,evidence)
                    VALUES(?,?,?,?,?,?,?)''',
                    (uid(),pid,REMOTE,REMOTE,'selfLink','','[]'))
        a=c.get('/api/projects/'+one['id']).json()['snapshot']
        b=c.get('/api/projects/'+two['id']).json()['snapshot']
        assert [x['id'] for x in a['entities']]==[REMOTE]
        assert [x['id'] for x in b['entities']]==[REMOTE]
        assert len(a['attributes'])==len(b['attributes'])==1
        assert a['attributes'][0]['project_id']!=b['attributes'][0]['project_id']
        # Deleting project A must not consult or delete project B's references.
        result=c.request('DELETE',f'/api/projects/{one["id"]}',json={
            'confirm_name':one['name'],'expected_revision':one['revision']})
        assert result.status_code==200,result.text
        assert result.json()['remote_unchanged']
        assert c.get('/api/projects/'+one['id']).status_code==404
        b2=c.get('/api/projects/'+two['id']).json()['snapshot']
        assert b2['entities'][0]['id']==REMOTE
        assert len(b2['attributes'])==len(b2['relations'])==1
        assert len(c.get('/api/projects').json())==1
        assert store.one('SELECT * FROM project_deletions WHERE project_id=?',(one['id'],))


def test_workspace_delete_requires_name_revision_and_only_removes_owned_upload(tmp_path,monkeypatch):
    with setup(monkeypatch,tmp_path) as c:
        one=project(c,'测试-保留数据');two=project(c,'其他项目')
        data=b'hello ontoflow'
        r=c.post('/api/projects/'+one['id']+'/documents',files={'file':('example.txt',data,'text/plain')})
        assert r.status_code==200,r.text
        path=c.app.state.store.one('SELECT path FROM documents WHERE project_id=?',(one['id'],))['path']
        from pathlib import Path
        assert Path(path).exists()
        endpoint=f'/api/projects/{one["id"]}'
        for request_body,code in [
            ({'confirm_name':'错误项目','expected_revision':one['revision']},422),
            ({'confirm_name':one['name'],'expected_revision':one['revision']+1},409),
        ]:
            rejected=c.request('DELETE',endpoint,json=request_body)
            assert rejected.status_code==code,rejected.text
            assert c.get(endpoint).status_code==200
            assert Path(path).exists()
        r=c.request('DELETE',endpoint,json={'confirm_name':one['name'],'expected_revision':one['revision']})
        assert r.status_code==200,r.text
        assert r.json()['file_cleanup_pending']==0
        assert not Path(path).exists()
        assert c.get("/api/projects/"+two["id"]).status_code==200
        assert c.request('DELETE',endpoint,json={'confirm_name':one['name'],'expected_revision':one['revision']}).status_code==404


def test_delete_refuses_active_jobs(tmp_path,monkeypatch):
    with setup(monkeypatch,tmp_path) as c:
        p=project(c,'运行中的工作空间')
        with c.app.state.store.tx() as conn:
            conn.execute('''INSERT INTO workflow_jobs
                (id,project_id,job_kind,stage,state,phase,completed_units,total_units,
                detail,error,source_fingerprint,result,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (uid(),p['id'],'summary','','running','running',0,1,'','', '', '{}',now(),now()))
        r=c.request('DELETE','/api/projects/'+p['id'],json={
            'confirm_name':p['name'],'expected_revision':p['revision']})
        assert r.status_code==409 and '任务' in r.text
        assert c.get('/api/projects/'+p['id']).status_code==200


def test_delete_cancels_queued_job_without_running_it(tmp_path,monkeypatch):
    with setup(monkeypatch,tmp_path) as c:
        p=project(c,'尚未执行')
        with c.app.state.store.tx() as conn:
            conn.execute('''INSERT INTO workflow_jobs
                (id,project_id,job_kind,stage,state,phase,completed_units,total_units,
                detail,error,source_fingerprint,result,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (uid(),p['id'],'summary','','queued','queued',0,1,'','','','{}',now(),now()))
        r=c.request('DELETE','/api/projects/'+p['id'],json={
            'confirm_name':p['name'],'expected_revision':p['revision']})
        assert r.status_code==200,r.text
        assert c.get('/api/projects/'+p['id']).status_code==404


def test_entity_ref_cannot_point_into_other_project(tmp_path,monkeypatch):
    with setup(monkeypatch,tmp_path) as c:
        a=project(c,'A');b=project(c,'B');stub(c,b['id'])
        with pytest.raises(Exception):
            with c.app.state.store.tx() as conn:
                conn.execute('''INSERT INTO attributes
                    (id,project_id,owner_id,name,datatype,required,multivalued,description,evidence)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (uid(),a['id'],REMOTE,'bad','string',0,0,'','[]'))
        assert c.app.state.store.one('SELECT COUNT(*) n FROM attributes WHERE project_id=?',(a['id'],))['n']==0


class FakeCursor:
    def __init__(self,definition='PRIMARY KEY (id)',invalid=0):
        self.calls=[];self.definition=definition;self.invalid=invalid;self.last=None
    def execute(self,sql,params=()):
        self.calls.append(sql.strip())
        if 'SELECT pg_get_constraintdef' in sql:self.last={'definition':self.definition}
        elif 'COUNT(*) AS n' in sql:self.last={'n':self.invalid}
        elif 'SELECT 1 AS ok FROM pg_constraint' in sql:self.last=None
        else:self.last=None
        return self
    def fetchone(self):return self.last


def test_pg_existing_global_pk_upgrade_uses_composite_fks():
    conn=FakeCursor()
    PgStore._ensure_workspace_entity_identity(conn)
    statements='\n'.join(conn.calls)
    assert 'DROP CONSTRAINT entities_pkey' in statements
    assert 'PRIMARY KEY(project_id,id)' in statements
    assert 'FOREIGN KEY(project_id,owner_id)' in statements
    assert 'FOREIGN KEY(project_id,source_id)' in statements
    assert 'FOREIGN KEY(project_id,target_id)' in statements
    new=FakeCursor(definition='PRIMARY KEY (project_id, id)')
    PgStore._ensure_workspace_entity_identity(new)
    assert not any('DROP CONSTRAINT entities_pkey' in sql for sql in new.calls)
    with pytest.raises(RuntimeError,match='拒绝自动修改约束'):
        PgStore._ensure_workspace_entity_identity(FakeCursor(invalid=1))
