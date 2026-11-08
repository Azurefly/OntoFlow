"""Cross-page >200 approval and read-only/re-approved sync conflict recovery.

Uses SQLite + mock remote, never touches a live OpenMind service or real data.
"""
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import uid,now,j
from app.task_leases import claim_job,Lease,lease_scope,LostLease
from test_review_fixes_v137 import prepare_sync_scenario

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    with TestClient(create_app(tmp_path/'state')) as c:yield c

def bootstrap(c,kind='entity',count=3,invalid_index=None):
    r=c.post('/api/projects',json={'name':'跨页审核'});assert r.status_code==200,r.text
    pid=r.json()['project']['id'];db=c.app.state.store
    owner=uid()
    stage={'entity':'ENTITY','attribute':'PROPERTY','relation':'RELATION'}[kind]
    ids=[]
    with db.tx() as sql:
        sql.execute('UPDATE projects SET stage=?,scope_confirmed=1,summary=?,scope=? WHERE id=?',(stage,'测试','本体类型',pid))
        if kind=='attribute':
            sql.execute('INSERT INTO entities(id,project_id,name,kind,description,evidence) VALUES(?,?,?,?,?,?)',(owner,pid,'测试设备','class','','[]'))
        for i in range(count):
            qid=uid();ids.append(qid)
            payload={'name':f'字段{i:04}', 'kind':'class'} if kind=='entity' else {'name':f'字段{i:04}','owner_id':owner,'datatype':'string'}
            if i==invalid_index:payload['owner_id']='missing_owner'
            sql.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(qid,pid,stage,kind,j(payload),'[]','human','pending',0,'',now(),None))
    return pid,ids

def preview(c,pid,kind='entity'):
    r=c.get(f'/api/projects/{pid}/proposals/review-all/preview',params={'kind':kind});assert r.status_code==200,r.text
    return r.json()

def start(c,pid,p):
    return c.post(f'/api/projects/{pid}/proposals/review-all',json={
        'kind':p['kind'],'expected_revision':p['revision'],'fingerprint':p['fingerprint'],
        'confirmation':'CONFIRM_ACCEPT_ALL_PENDING'})

def wait(c,pid,jid):
    for _ in range(500):
        r=c.get(f'/api/projects/{pid}/jobs/{jid}');assert r.status_code==200,r.text
        job=r.json()['job']
        if job['state'] not in ('queued','running'):return job,json.loads(job['result'])
        time.sleep(.02)
    raise AssertionError('review-all did not finish')

def test_318_attributes_cross_pages_partial_success(client):
    pid,ids=bootstrap(client,'attribute',318,143)
    assert client.get(f'/api/projects/{pid}/proposals/page?kind=attribute&page_size=20').json()['total']==318
    p=preview(client,pid,'attribute');assert p['total']==318
    r=start(client,pid,p);assert r.status_code==202,r.text
    job,report=wait(client,pid,r.json()['job']['id'])
    assert job['state']=='partial',job
    assert (report['cursor'],report['succeeded'],report['failed'],report['unprocessed'])==(318,317,1,0)
    assert report['failures'][0]['id']==ids[143]
    state=client.get('/api/projects/'+pid).json()
    assert len(state['snapshot']['attributes'])==317
    assert state['pending_totals']['attribute']==1
    assert not state['releases']  # local draft approvals are NOT remote publish
    assert not client.app.state.store.many('SELECT * FROM openmind_operations WHERE project_id=?',(pid,))
    assert any('318' in m['content'] and '同意' in m['content'] for m in state['messages'])

def test_preview_snapshot_detects_new_candidate_before_confirm(client):
    pid,_=bootstrap(client,count=2);p=preview(client,pid)
    r=client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'新增','kind':'class'}})
    assert r.status_code==200,r.text
    assert start(client,pid,p).status_code==409
    assert len(client.get('/api/projects/'+pid).json()['snapshot']['entities'])==0

def test_new_items_during_execution_are_not_in_frozen_snapshot(client,monkeypatch):
    import app.main as mod
    # Hold the SQLite background runner; the normal worker runner is then invoked manually.
    real_thread=mod.threading.Thread
    class NoStart:
        def __init__(self,*a,**kw):pass
        def start(self):pass
    pid,_=bootstrap(client,count=2);p=preview(client,pid)
    monkeypatch.setattr(mod.threading,'Thread',NoStart)
    response=start(client,pid,p);assert response.status_code==202,response.text
    monkeypatch.setattr(mod.threading,'Thread',real_thread)
    job=response.json()['job']
    r=client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'后来新增','kind':'class'}})
    assert r.status_code==200,r.text
    client.app.state.run_job(pid,job['id'],'review_all','ENTITY',job['source_fingerprint'],{},'test')
    state=client.get('/api/projects/'+pid).json()
    assert len(state['snapshot']['entities'])==2
    assert state['pending_totals']['entity']==1

def test_review_context_change_stops_instead_of_blind_accept(client,monkeypatch):
    import app.main as mod
    real_thread=mod.threading.Thread
    class NoStart:
        def __init__(self,*a,**kw):pass
        def start(self):pass
    pid,_=bootstrap(client,count=3);p=preview(client,pid)
    monkeypatch.setattr(mod.threading,'Thread',NoStart)
    response=start(client,pid,p);assert response.status_code==202,response.text
    monkeypatch.setattr(mod.threading,'Thread',real_thread)
    job=response.json()['job']
    with client.app.state.store.tx() as sql:sql.execute('UPDATE projects SET revision=revision+1 WHERE id=?',(pid,))
    client.app.state.run_job(pid,job['id'],'review_all','ENTITY',job['source_fingerprint'],{},'test')
    _,result=wait(client,pid,job['id'])
    assert result['unprocessed']==3 and result['succeeded']==0
    assert result['stopped_reason']

def _create_conflict(c,monkeypatch):
    pid,remote,writes,reads,body=prepare_sync_scenario(c,monkeypatch)
    assert c.post(f'/api/projects/{pid}/openmind/sync',json=body).status_code==502
    original=copy.deepcopy(remote['entities'][0])
    remote['entities'][0]['description']='远端人工改动，不可自动覆盖'
    remote['entities'][0]['lockVersion']+=1
    r=c.post(f'/api/projects/{pid}/openmind/sync',json=body)
    assert r.status_code==409,r.text
    return pid,remote,writes,body,original

def test_conflict_shows_object_and_real_field_difference_without_write(client,monkeypatch):
    pid,remote,writes,body,original=_create_conflict(client,monkeypatch)
    before=copy.deepcopy(remote)
    checked=client.post(f'/api/projects/{pid}/openmind/conflicts/check')
    assert checked.status_code==200,checked.text
    out=checked.json();assert out['remote_writes']==0 and out['remaining']==1
    op=out['items'][0]
    assert op['name']=='设备' and op['code'] and op['state']=='CONFLICT'
    assert any(d['field']=='description' and d['actual']=='远端人工改动，不可自动覆盖' for d in op['differences'])
    assert remote==before and writes==['entity']

def test_sticky_conflict_recovers_when_remote_matches_approved(client,monkeypatch):
    pid,remote,writes,body,original=_create_conflict(client,monkeypatch)
    remote['entities'][0]['description']=original['description']
    r=client.post(f'/api/projects/{pid}/openmind/conflicts/check')
    assert r.status_code==200,r.text
    assert r.json()['remaining']==0 and r.json()['reconciled']==1
    assert writes==['entity']
    assert client.app.state.store.one('SELECT state FROM openmind_operations WHERE project_id=?',(pid,))['state']=='CONFIRMED'

def test_conflict_not_applied_can_be_reconciled_without_network_write(client,monkeypatch):
    pid,remote,writes,body,original=_create_conflict(client,monkeypatch)
    remote['entities'].clear()  # write was undone remotely, now matching the pre-create state
    r=client.post(f'/api/projects/{pid}/openmind/conflicts/check')
    assert r.status_code==200,r.text
    assert r.json()['remaining']==0 and writes==['entity']
    assert client.app.state.store.one('SELECT state FROM openmind_operations WHERE project_id=?',(pid,))['state']=='NOT_APPLIED'

def test_conflict_reapproval_requires_fresh_read_and_invalidates_old_publication(client,monkeypatch):
    pid,remote,writes,old_body,original=_create_conflict(client,monkeypatch)
    checked=client.post(f'/api/projects/{pid}/openmind/conflicts/check').json();op=checked['items'][0]
    request={'expected_revision':checked['revision'],'observation_hash':op['observation_hash'],
             'confirmation':'KEEP_REMOTE_AND_REVIEW_AGAIN','note':'已核对差异，保留远端，重新审核本地变更'}
    r=client.post(f'/api/projects/{pid}/openmind/operations/{op["id"]}/return-to-review',json=request)
    assert r.status_code==200,r.text
    assert r.json()['remote_writes']==0 and r.json()['requires_republish']
    assert writes==['entity'] and remote['entities'][0]['description']=='远端人工改动，不可自动覆盖'
    assert client.post(f'/api/projects/{pid}/openmind/sync',json=old_body).status_code==409
    assert client.post(f'/api/projects/{pid}/openmind/baseline/refresh').status_code==200
    release=client.post(f'/api/projects/{pid}/publish',json={'note':'复核差异后批准本地定义'})
    assert release.status_code==200,release.text
    new_body={'expected_revision':release.json()['revision'],'confirmation':'CONFIRM_OPENMIND_WRITE'}
    r=client.post(f'/api/projects/{pid}/openmind/sync',json=new_body)
    assert r.status_code==200,r.text
    assert writes==['entity','entity']  # only the later explicit publish+sync authorizes write
    assert len(remote['entities'])==1

def test_stale_observation_rejected_without_clearing_conflict(client,monkeypatch):
    pid,remote,writes,old_body,original=_create_conflict(client,monkeypatch)
    checked=client.post(f'/api/projects/{pid}/openmind/conflicts/check').json();op=checked['items'][0]
    remote['entities'][0]['description']='核对后又修改'
    r=client.post(f'/api/projects/{pid}/openmind/operations/{op["id"]}/return-to-review',json={
        'expected_revision':checked['revision'],'observation_hash':op['observation_hash'],
        'confirmation':'KEEP_REMOTE_AND_REVIEW_AGAIN','note':'核对后准备重新审批'})
    assert r.status_code==409,r.text
    assert client.app.state.store.one('SELECT state FROM openmind_operations WHERE project_id=?',(pid,))['state']=='CONFLICT'
    assert writes==['entity']

def test_foreign_project_cannot_resolve_operation(client,monkeypatch):
    pid,remote,writes,old_body,original=_create_conflict(client,monkeypatch)
    checked=client.post(f'/api/projects/{pid}/openmind/conflicts/check').json();op=checked['items'][0]
    # A nonexisting workspace can't access the operation even knowing its hash.
    r=client.post(f'/api/projects/another-workspace/openmind/operations/{op["id"]}/return-to-review',json={
        'expected_revision':checked['revision'],'observation_hash':op['observation_hash'],
        'confirmation':'KEEP_REMOTE_AND_REVIEW_AGAIN','note':'尝试跨工作空间'})
    assert r.status_code in (404,409) and writes==['entity']

def test_review_resume_keeps_committed_items_once(client,monkeypatch):
    import app.main as mod
    import app.review_all as review_module
    real_thread=mod.threading.Thread
    class NoStart:
        def __init__(self,*a,**kw):pass
        def start(self):pass
    pid,_=bootstrap(client,count=4);p=preview(client,pid)
    monkeypatch.setattr(mod.threading,'Thread',NoStart)
    response=start(client,pid,p);assert response.status_code==202,response.text
    monkeypatch.setattr(mod.threading,'Thread',real_thread)
    job=response.json()['job'];original=review_module.run_review_all
    calls=[]
    def interrupted(store,pid,jid,stage,apply,announce):
        def partly_apply(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise LostLease('simulated worker interruption')
            return apply(*args,**kwargs)
        return original(store,pid,jid,stage,partly_apply,announce)
    monkeypatch.setattr(review_module,'run_review_all',interrupted)
    with pytest.raises(LostLease):
        client.app.state.run_job(pid,job['id'],'review_all','ENTITY',job['source_fingerprint'],{},'test')
    db=client.app.state.store
    saved=db.one('SELECT * FROM workflow_jobs WHERE id=?',(job['id'],))
    assert json.loads(saved['result'])['cursor']==1
    assert len(db.many('SELECT * FROM entities WHERE project_id=?',(pid,)))==1
    from app.task_leases import timestamp
    with db.tx() as c:c.execute('UPDATE workflow_jobs SET lease_until=? WHERE id=?',(timestamp(-5),job['id']))
    monkeypatch.setattr(review_module,'run_review_all',original)
    client.app.state.run_job(pid,job['id'],'review_all','ENTITY',job['source_fingerprint'],{},'test')
    _,report=wait(client,pid,job['id'])
    assert report['succeeded']==4 and report['failed']==0 and report['cursor']==4
    assert len(db.many('SELECT * FROM entities WHERE project_id=?',(pid,)))==4
    assert len(db.many("SELECT * FROM audit WHERE project_id=? AND action='proposal_all_accept_item'",(pid,)))==4


def test_all_accept_requires_explicit_confirmation(client):
    pid,_=bootstrap(client,count=3);p=preview(client,pid)
    r=client.post(f'/api/projects/{pid}/proposals/review-all',json={
        'kind':'entity','expected_revision':p['revision'],'fingerprint':p['fingerprint'],'confirmation':''})
    assert r.status_code==422
    assert len(client.app.state.store.many('SELECT * FROM entities WHERE project_id=?',(pid,)))==0


def test_snapshot_candidate_content_change_not_accepted(client,monkeypatch):
    import app.main as mod
    real_thread=mod.threading.Thread
    class NoStart:
        def __init__(self,*a,**kw):pass
        def start(self):pass
    pid,ids=bootstrap(client,count=2);p=preview(client,pid)
    monkeypatch.setattr(mod.threading,'Thread',NoStart)
    response=start(client,pid,p)
    monkeypatch.setattr(mod.threading,'Thread',real_thread)
    job=response.json()['job']
    with client.app.state.store.tx() as c:
        c.execute('UPDATE proposals SET payload=? WHERE id=?',(j({'name':'确认后更改','kind':'class'}),ids[0]))
    client.app.state.run_job(pid,job['id'],'review_all','ENTITY',job['source_fingerprint'],{},'test')
    _,out=wait(client,pid,job['id'])
    assert out['succeeded']==1 and out['failed']==1
    assert '内容已改变' in out['failures'][0]['reason']


def test_conflict_identity_change_cannot_be_acknowledged_as_same_object(client,monkeypatch):
    pid,remote,writes,old_body,original=_create_conflict(client,monkeypatch)
    remote['entities'][0]['code']='external_renamed'
    checked=client.post(f'/api/projects/{pid}/openmind/conflicts/check').json();op=checked['items'][0]
    assert not op['can_return_to_review']
    r=client.post(f'/api/projects/{pid}/openmind/operations/{op["id"]}/return-to-review',json={
        'expected_revision':checked['revision'],'observation_hash':op['observation_hash'],
        'confirmation':'KEEP_REMOTE_AND_REVIEW_AGAIN','note':'身份冲突不能忽略'})
    assert r.status_code==409 and writes==['entity']


def test_remote_preview_is_not_ready_when_conflict_remains(client,monkeypatch):
    pid,remote,writes,old_body,original=_create_conflict(client,monkeypatch)
    r=client.get(f'/api/projects/{pid}/openmind/plan?remote=true')
    assert r.status_code==200,r.text
    assert not r.json()['ready']
    assert any('写入冲突' in reason for reason in r.json()['blockers'])
    assert writes==['entity']
