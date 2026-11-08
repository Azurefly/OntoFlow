"""Regression for frozen v1.3.6 review A01-A15. Assertions describe FIXED behavior.

No live accounts/remote network. SQLite tests are not substitutes for PG lock tests.
The old bug reproducer remains immutable in the audit archive.
"""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from app import brain, incremental
from app.main import create_app
from app.store import Store, uid, now, j
from app.json_discovery import extract_json_obj, native_chunks
from app.openmind_baseline import canonical_catalog, duplicate_against_baseline
from app.openmind import OpenMindClient, OpenMindError, make_plan
from app.native_semantics import semantically_equal, prepare_write
from app.task_leases import Lease, LostLease, claim_job, heartbeat, lease_scope, timestamp
from app.sync_engine import SyncSession, SyncBusy

D='10000000-0000-4000-8000-000000000001';B='10000000-0000-4000-8000-000000000002'
E1='20000000-0000-4000-8000-000000000001';E2='20000000-0000-4000-8000-000000000002'
A='30000000-0000-4000-8000-000000000001';R='40000000-0000-4000-8000-000000000001'
CONFIG={'base_url':'http://model.invalid/v1','model':'stub','api_key':''}

@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in ('DATABASE_URL','API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY','OPENMIND_BASE_URL',
                'OPENMIND_ACCESS_TOKEN','OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_AUTH_MODE',
                'LLM_BASE_URL','LLM_MODEL','OPENMIND_ADMIN_TOKEN','MODEL_ALLOWED_HOSTS','ASYNC_PARSE',
                'STRUCTURED_EXTRACTION'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('APP_ENV','development');monkeypatch.setenv('ONTOFLOW_DEMO_MODE','true')

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'db'),raise_server_exceptions=True) as c:yield c

def project(c):
    r=c.post('/api/projects',json={'name':'独立修复验收'})
    assert r.status_code==200,r.text
    return r.json()['project']['id']

def upload(c,pid,name='one.txt',text='实体：设备'):
    return c.post(f'/api/projects/{pid}/documents',files={'file':(name,text.encode(),'text/plain')})

def catalog():
    return {'schemaVersion':13,'domain':{'id':D,'name':'合成本体','activeBranchId':B},
        'entities':[{'id':E1,'domainId':D,'branchId':B,'name':'人员类型','code':'person','parentId':None},
                    {'id':E2,'domainId':D,'branchId':B,'name':'组织类型','code':'organization','parentId':None}],
        'attributes':[],'actions':[],'capabilities':[],
        'relations':[{'id':R,'domainId':D,'branchId':B,'name':'人员组织关联','code':'member_of',
            'sourceEntityIds':[E1],'targetEntityIds':[E2],'executionMode':'SEMANTIC',
            'steps':'依据权威人员组织编号匹配','symmetric':False,
            'dependencies':[{'kind':'entity','id':E1,'required':True}]}]}

def as_piece(ch):
    return {'document_id':'doc','project_id':'p','content':ch['text'],'content_hash':incremental.sha(ch['text']),
            'kind':ch.get('kind',''),'section':ch.get('section',''),'locator':ch.get('locator','')}

def enqueue(db,pid,kind='summary',stage='',signature='source'):
    jid=uid()
    with db.tx() as c:
        c.execute('''INSERT INTO workflow_jobs(id,project_id,job_kind,stage,state,phase,
            source_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (jid,pid,kind,stage,'queued','queued',signature,now(),now()))
    return jid


def test_A01_multiple_document_queues_are_distinct_and_duplicate_file_is_idempotent(client,monkeypatch):
    pid=project(client);db=client.app.state.store
    monkeypatch.setenv('ASYNC_PARSE','1')
    one=upload(client,pid,'first.txt','实体：设备')
    two=upload(client,pid,'second.txt','实体：部件')
    assert one.status_code==two.status_code==200,(one.text,two.text)
    assert len(db.many('SELECT * FROM documents WHERE project_id=?',(pid,)))==2
    jobs=db.many('SELECT * FROM workflow_jobs WHERE project_id=?',(pid,))
    assert len(jobs)==2 and len({j['source_fingerprint'] for j in jobs})==2
    assert len({j['stage'] for j in jobs})==2
    dup=upload(client,pid,'third.txt','实体：设备')
    assert dup.status_code==409 and len(list(db.uploads.iterdir()))==2


def test_A01_enqueue_failure_does_not_leave_orphan_file(client,monkeypatch):
    pid=project(client);db=client.app.state.store;monkeypatch.setenv('ASYNC_PARSE','1')
    with db.tx() as c:
        c.execute("CREATE TRIGGER fail_job BEFORE INSERT ON workflow_jobs BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
    result=upload(client,pid)
    assert result.status_code==503,result.text
    assert db.many('SELECT * FROM documents WHERE project_id=?',(pid,))==[]
    assert list(db.uploads.iterdir())==[]


def test_A02_old_worker_cannot_write_any_state_or_model_results(client,monkeypatch):
    pid=project(client);assert upload(client,pid).status_code==200
    db=client.app.state.store;cfg=client.app.state.model_settings.effective()
    sig=client.app.state.source_signature(pid,'summary','',cfg)
    jid=enqueue(db,pid,signature=sig)
    job=claim_job(db,'old',jid);lease=Lease(jid,'old',job['lease_generation'])
    def reclaimed(*args,**kwargs):
        with lease_scope(None):
            with db.tx() as c:
                c.execute("UPDATE workflow_jobs SET worker_id='new',lease_generation=lease_generation+1,lease_until=? WHERE id=?",(timestamp(120),jid))
        return {'summary':'不允许旧Worker提交','scope':'旧结果','coverage':{}}
    monkeypatch.setattr(brain,'analyze',reclaimed)
    with pytest.raises(LostLease):client.app.state.run_job(pid,jid,'summary','',sig,cfg,'test',lease=lease)
    current=db.project(pid)
    assert current['candidate_summary']!='不允许旧Worker提交'
    state=db.one('SELECT * FROM workflow_jobs WHERE id=?',(jid,))
    assert state['worker_id']=='new' and state['state']=='running'
    assert not heartbeat(db,lease)
    with lease_scope(lease):
        with pytest.raises(LostLease):
            with db.tx() as c:c.execute('UPDATE projects SET name=? WHERE id=?',('BAD',pid))
    assert db.project(pid)['name']!='BAD'


def test_A02_claim_generation_and_expiry_rollback(client):
    pid=project(client);db=client.app.state.store;jid=enqueue(db,pid)
    first=claim_job(db,'one',jid)
    assert claim_job(db,'two',jid) is None
    with db.tx() as c:c.execute('UPDATE workflow_jobs SET lease_until=? WHERE id=?',(timestamp(-10),jid))
    second=claim_job(db,'two',jid)
    assert second['lease_generation']==first['lease_generation']+1
    old=Lease(jid,'one',first['lease_generation'])
    with lease_scope(old),pytest.raises(LostLease):
        with db.tx():pass
    fresh=Lease(jid,'two',second['lease_generation'])
    with lease_scope(fresh),pytest.raises(LostLease):
        with db.tx() as c:
            c.execute('UPDATE projects SET name=? WHERE id=?',('MUST ROLLBACK',pid))
            c.execute('UPDATE workflow_jobs SET lease_until=? WHERE id=?',(timestamp(-10),jid))
    assert db.project(pid)['name']!='MUST ROLLBACK'


def test_A02_simultaneous_claim_only_one_owner(client):
    pid=project(client);db=client.app.state.store;jid=enqueue(db,pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(lambda owner:claim_job(db,owner,jid),['one','two']))
    assert sum(x is not None for x in result)==1


@pytest.mark.parametrize('change',['dependency','execution_spec'])
def test_A04_full_relation_semantics_not_counts(client,change):
    source=catalog()
    if change=='execution_spec':source['relations'][0].update(executionMode='DETERMINISTIC',executionSpec={
        'schema':'openmind.execution-spec/v3','version':3,'parameters':[],
        'steps':[{'specStepId':'q','kind':'ONTOLOGY_QUERY','targetId':E1,'dependsOn':[],'bindings':{},'options':{}}],
        'outputs':{'result':{'kind':'step_output','stepId':'q','port':'return'}}})
    target=copy.deepcopy(source)
    if change=='dependency':target['relations'][0]['dependencies'][0]['id']=E2
    else:target['relations'][0]['executionSpec']['steps'][0]['targetId']=E2
    p=as_piece(next(ch for ch in native_chunks(source) if ch['kind']=='native-relation'))
    clues=brain.joint_extract_segment(p);empty={'entities':[],'attributes':[],'relations':[]}
    out,gaps=incremental.align_clues('RELATION',clues,empty,p,baseline=canonical_catalog(target))
    assert out==[] and gaps
    same,same_gaps=incremental.align_clues('RELATION',clues,empty,p,baseline=canonical_catalog(source))
    assert same==[] and same_gaps==[]


def test_A05_all_json_narratives_including_late_nested_fields_are_covered():
    marker='后半文档独特类型定义'
    obj={'业务记录':[{'描述':f'第{i}条叙述','序号':i,'附录':{'案件描述':marker+str(i)}} for i in range(65)]}
    chunks=extract_json_obj(obj,'full.json');text='\n'.join(ch['text'] for ch in chunks)
    assert marker+'64' in text and '第64条叙述' in text
    prose=[c for c in chunks if c['kind']=='prose']
    assert len(prose)==130
    assert len({c['locator'] for c in prose})==130


def test_A06_top_level_and_nested_explicit_attributes_are_not_lost():
    obj={'entities':[{'id':'E01','name':'设备类型','kind':'class','properties':{'名称':'string'}}],
         'attributes':[{'name':'设备状态','owner':'E01','dataType':'string'}], 'relations':[]}
    chunks=extract_json_obj(obj,'explicit.json');text='\n'.join(c['text'] for c in chunks)
    assert '设备状态' in text and '名称' in text
    assert len([c for c in chunks if c['kind']=='json-field'])==2
    invalid=copy.deepcopy(obj);invalid['attributes'][0]['owner']='未知实体'
    with pytest.raises(Exception):extract_json_obj(invalid,'bad.json')


def test_A07_retryable_checkpoint_uses_new_response_then_completes(tmp_path,monkeypatch):
    calls=[];db=Store(tmp_path/'cache')
    p={'document_id':'d','content':'设备类型包含通用属性和关系。','kind':'prose','section':'定义','locator':'L1'}
    p['content_hash']=incremental.sha(p['content'])
    def bad(*args):calls.append('bad');return {'entities':[{'name':'设备','quote':'不存在'}],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',bad)
    with brain.using_model(CONFIG):out,_=incremental.cached_clues(db,p,CONFIG,brain.joint_extract_segment)
    assert out['_state']=='retryable' and out['_retry_units'] and not out['_complete']
    def good(*args):calls.append('good');return {'entities':[{'name':'设备类型','quote':'设备类型'}],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',good)
    with brain.using_model(CONFIG):final,_=incremental.cached_clues(db,p,CONFIG,brain.joint_extract_segment)
    assert calls==['bad','good'] and final['_complete'] and final['_state']=='complete'


def test_A07_retry_budget_stops_and_explicit_reset_is_source_scoped(tmp_path,monkeypatch):
    db=Store(tmp_path/'cache');calls=[]
    p={'content':'设备对象的属性定义','kind':'prose','content_hash':incremental.sha('设备对象的属性定义')}
    def bad(*args):calls.append(1);return {'entities':[{'name':'设备','quote':'假的'}],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',bad)
    with brain.using_model(CONFIG):
        for _ in range(5):out,_=incremental.cached_clues(db,p,CONFIG,brain.joint_extract_segment)
        before=len(calls)
        assert out['_state']=='terminal_invalid' and before<=4
        incremental.cached_clues(db,p,CONFIG,brain.joint_extract_segment,force_retry=True)
    assert len(calls)==before+1


def test_A08_property_name_match_cannot_hide_type_change():
    base=catalog();base['attributes']=[{'id':A,'branchId':B,'domainId':D,'name':'年龄','code':'age','dataType':'string','entityIds':[E1]}]
    with pytest.raises(OpenMindError,match='类型|冲突'):
        duplicate_against_baseline('attribute',{'name':'年龄','owner_id':E1,'datatype':'integer'},canonical_catalog(base))
    assert duplicate_against_baseline('attribute',{'name':'年龄','owner_id':E1,'datatype':'string'},canonical_catalog(base))


def shared_plans():
    es=[{'id':E1,'name':'设备','kind':'class','description':'','class_id':None,'evidence':[]},
        {'id':E2,'name':'部件','kind':'class','description':'','class_id':None,'evidence':[]}]
    def attr(identity,owner):return {'id':identity,'owner_id':owner,'name':'编号','datatype':'string',
        'required':False,'multivalued':False,'description':'对象编号',
        'evidence':[{'verified':True,'locator':'$.attributes[0].entityIds[0]',
                    'source_code':'shared_id','source_name':'编号','quote':'编号'}]}
    first={'project':{'revision':1},'entities':es,'attributes':[attr('a1',E1)],'relations':[]}
    plan1=make_plan(first,'project',D,B);assert plan1['ready']
    remote={'entities':[],'attributes':[],'relations':[]};mappings={}
    for step in plan1['steps']:
        remote[{'entity':'entities','attribute':'attributes','relation':'relations'}[step['kind']]].append({**step['data'],'lockVersion':1})
        mappings[(step['kind'],step['local_id'])]={'remote_id':step['data']['id'],'code':step['data']['code']}
    second=copy.deepcopy(first);second['attributes'].append(attr('a2',E2));second['project']['revision']=2
    return make_plan(second,'project',D,B,mappings=mappings,catalog=remote),remote


def test_A09_additive_shared_attribute_binding_write_matches_preview():
    plan,remote=shared_plans();assert plan['ready'],plan
    step=next(s for s in plan['steps'] if s['kind']=='attribute');requests=[]
    remote['attributes'][0]['transform']='preserve metadata'
    def handler(req):
        payload=json.loads(req.content);requests.append((req.method,payload))
        return httpx.Response(200,json={**payload['data'],'lockVersion':2})
    c=OpenMindClient('http://test.invalid','t',transport=httpx.MockTransport(handler))
    try:response=c.write(B,step,remote['attributes'][0])
    finally:c.close()
    assert requests[0][0]=='PUT' and len(requests[0][1]['data']['entityIds'])==2
    assert requests[0][1]['data']['transform']=='preserve metadata'
    assert requests[0][1]['data']['lockVersion']==1 and response['lockVersion']==2
    bad=copy.deepcopy(step);bad['approved_binding_additions']=[]
    with pytest.raises(OpenMindError):prepare_write(bad,remote['attributes'][0])
    bad=copy.deepcopy(step);bad['data']['entityIds']=[]
    with pytest.raises(OpenMindError):prepare_write(bad,remote['attributes'][0])


def test_A10_library_overview_uses_only_current_domain():
    from app.structured_discovery import markdown_chunks
    from app.readable_overview import structured_overview
    text='''# 图书借阅本体设计
## 实体定义
| 实体名称 | 实体编号 | 描述 |
|---|---|---|
| 图书 | book | 可借阅图书 |
| 借阅人 | borrower | 人员类型 |
| 借阅记录 | loan | 业务记录 |
## 实体关系
### 7.1 REL-01｜借阅人 → 图书｜借阅（1:N）
### 7.2 REL-02｜借阅记录 → 借阅人｜属于（N:1）
### 7.3 REL-03｜借阅记录 → 图书｜对应（N:1）'''
    result=structured_overview([{'filename':'library.md','chunks':markdown_chunks(text)}])
    assert result and '图书' in result['summary']
    assert all(t not in result['summary'] for t in ('信道链路','分发记录','告警','用户终端'))


@pytest.mark.parametrize('name',['设备"类别"',r'设备\类别'])
def test_A11_native_escaped_name_preserves_candidate_and_completion(name):
    obj=catalog();obj['entities'][0]['name']=name
    ch=next(c for c in native_chunks(obj) if c['kind']=='native-entity')
    result=brain.joint_extract_segment(as_piece(ch))
    assert [v['name'] for v in result['entities']]==[name]
    assert result['_complete'] and not result['_unresolved']
    assert result['entities'][0]['quote'] in ch['text']


@pytest.mark.parametrize('key',['branchId','domainId'])
def test_A12_invalid_scope_UUID_is_rejected_at_input_boundary(key):
    obj=catalog()
    for group in ('entities','attributes','relations'):
        for item in obj[group]:item[key]='not-a-uuid'
    with pytest.raises(Exception,match='UUID'):native_chunks(obj)


def test_A13_successful_segment_is_reviewable_before_another_finishes(client,monkeypatch):
    monkeypatch.setenv('STRUCTURED_EXTRACTION','1');pid=project(client);db=client.app.state.store
    assert upload(client,pid).status_code==200
    assert client.put(f'/api/projects/{pid}/topic',json={'summary':'设备','scope':'设备类型'}).status_code==200
    with db.tx() as c:c.execute('UPDATE documents SET chunks=? WHERE project_id=?',(j([
        {'kind':'prose','section':'定义','locator':'L1','text':'实体：设备'},
        {'kind':'prose','section':'定义','locator':'L2','text':'实体：部件'}]),pid))
    release=threading.Event();waiting=threading.Event()
    def extractor(p,checkpoint=None,on_checkpoint=None):
        if p['locator']=='L2':
            waiting.set();release.wait(8);raise ValueError('单元二模拟失败')
        return {'entities':[{'name':'设备','quote':'设备'}],'attributes':[],'relations':[],
                '_complete':True,'_pending':[],'_unresolved':[],'_coverage':{}}
    monkeypatch.setattr(brain,'joint_extract_segment',extractor)
    job=client.post(f'/api/projects/{pid}/jobs/start?kind=propose').json()['job']
    try:
        assert waiting.wait(2)
        for _ in range(150):
            pending=db.one('SELECT COUNT(*) AS n FROM proposals WHERE project_id=?',(pid,))['n']
            if pending:break
            time.sleep(.02)
        assert pending==1
        assert db.one('SELECT state FROM workflow_jobs WHERE id=?',(job['id'],))['state']=='running'
    finally:release.set()
    for _ in range(150):
        done=db.one('SELECT * FROM workflow_jobs WHERE id=?',(job['id'],))
        if done['state'] not in ('queued','running'):break
        time.sleep(.02)
    assert done['state']=='partial',done
    assert len(json.loads(done['result'])['failed_segments'])==1
    assert db.one('SELECT COUNT(*) n FROM proposals WHERE project_id=?',(pid,))['n']==1


def test_A14_health_version_has_one_authoritative_source(client):
    from app.version import VERSION
    assert client.get('/api/health').json()['version']==client.app.version==VERSION
    assert VERSION==VERSION


def test_A15_independent_auditor_is_importable_from_bundle():
    from scripts.ontoflow_independent_audit import raw_reference
    assert callable(raw_reference)


def prepare_sync_scenario(client,monkeypatch,lose_response=True):
    import app.main as mod
    remote={'entities':[],'attributes':[],'relations':[],'actions':[],'capabilities':[]}
    writes=[];reads=[]
    class FakeRemote:
        def __init__(self,*args,**kwargs):pass
        def close(self):pass
        def catalog(self,*args):reads.append(1);return copy.deepcopy(remote)
        def graph(self,*args):return {'nodes':[], 'edges':[]}
        def write(self,branch,step,existing=None):
            row=prepare_write(step,existing);row['lockVersion']=(existing or {}).get('lockVersion',0)+1
            rows=remote[{'entity':'entities','attribute':'attributes','relation':'relations'}[step['kind']]]
            rows[:]=[x for x in rows if x['id']!=row['id']]+[row]
            writes.append(step['kind'])
            if lose_response and len(writes)==1:
                raise OpenMindError('模拟远端已提交，但响应超时丢失')
            return copy.deepcopy(row)
    pid=project(client)
    monkeypatch.setenv('OPENMIND_BASE_URL','https://review.invalid')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','synthetic-token')
    monkeypatch.setattr(mod,'OpenMindClient',FakeRemote)
    bound=client.put(f'/api/projects/{pid}/openmind/binding',json={'domain_id':D,'branch_id':B})
    assert bound.status_code==200,bound.text
    assert upload(client,pid).status_code==200
    assert client.put(f'/api/projects/{pid}/topic',json={'summary':'设备类型','scope':'设备类别'}).status_code==200
    p=client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'设备','kind':'class'}})
    assert p.status_code==200,p.text
    assert client.post(f'/api/projects/{pid}/proposals/{p.json()["created"][0]}/decision',json={'action':'accept'}).status_code==200
    for stage in ('PROPERTY','RELATION','REVIEW'):
        assert client.post(f'/api/projects/{pid}/stage',json={'target':stage}).status_code==200
    published=client.post(f'/api/projects/{pid}/publish',json={'note':'独立恢复验收'})
    assert published.status_code==200,published.text
    body={'expected_revision':published.json()['revision'],'confirmation':'CONFIRM_OPENMIND_WRITE'}
    return pid,remote,writes,reads,body


@pytest.mark.parametrize('recover_by',['retry','refresh'])
def test_A03_committed_response_loss_reconciles_no_duplicate_POST(client,monkeypatch,recover_by):
    pid,remote,writes,reads,body=prepare_sync_scenario(client,monkeypatch)
    first=client.post(f'/api/projects/{pid}/openmind/sync',json=body)
    assert first.status_code==502 and len(remote['entities'])==1,first.text
    db=client.app.state.store
    operation=db.one('SELECT * FROM openmind_operations WHERE project_id=?',(pid,))
    assert operation['state']=='UNKNOWN' and operation['attempts']==1
    assert json.loads(operation['desired'])['id']==remote['entities'][0]['id']
    assert db.many('SELECT * FROM openmind_mappings WHERE project_id=?',(pid,))==[]
    if recover_by=='refresh':
        refresh=client.post(f'/api/projects/{pid}/openmind/baseline/refresh')
        assert refresh.status_code==200,refresh.text
    second=client.post(f'/api/projects/{pid}/openmind/sync',json=body)
    assert second.status_code==200,second.text
    assert writes==['entity']
    assert second.json()['http_writes']==0
    assert len(db.many('SELECT * FROM openmind_mappings WHERE project_id=?',(pid,)))==1
    assert db.one('SELECT state FROM openmind_operations WHERE project_id=?',(pid,))['state']=='CONFIRMED'
    assert second.json()['catalog_reads']==2  # once before, once to verify, not per object


def test_A03_conflicting_unknown_result_frozen_not_overwritten(client,monkeypatch):
    pid,remote,writes,reads,body=prepare_sync_scenario(client,monkeypatch)
    assert client.post(f'/api/projects/{pid}/openmind/sync',json=body).status_code==502
    remote['entities'][0]['description']='外部用户更新，不能覆盖'
    remote['entities'][0]['lockVersion']=2
    second=client.post(f'/api/projects/{pid}/openmind/sync',json=body)
    assert second.status_code==409,second.text
    assert writes==['entity'] and remote['entities'][0]['description']=='外部用户更新，不能覆盖'
    op=client.app.state.store.one('SELECT state FROM openmind_operations WHERE project_id=?',(pid,))
    assert op['state']=='CONFLICT'


def test_A03_unrelated_external_drift_requires_fresh_review(client,monkeypatch):
    pid,remote,writes,reads,body=prepare_sync_scenario(client,monkeypatch)
    assert client.post(f'/api/projects/{pid}/openmind/sync',json=body).status_code==502
    remote['entities'].append({'id':E1,'name':'外部新增','code':'external','parentId':None,'domainId':D,'branchId':B,'lockVersion':1})
    second=client.post(f'/api/projects/{pid}/openmind/sync',json=body)
    assert second.status_code==409 and '非本次' in second.text,second.text
    assert writes==['entity']


def test_sync_exclusion_and_expired_owner_cannot_release_new_lock(client):
    pid=project(client);db=client.app.state.store
    one=SyncSession(db,pid,'endpoint',B,0,'test').acquire()
    with pytest.raises(SyncBusy):SyncSession(db,pid,'endpoint',B,0,'test').acquire()
    with db.tx() as c:c.execute('UPDATE sync_locks SET lease_until=? WHERE project_id=?',(timestamp(-5),pid))
    two=SyncSession(db,pid,'endpoint',B,0,'test').acquire()
    assert two.generation==one.generation+1
    with pytest.raises(SyncBusy):
        with one.transaction():pass
    one.release('failed',{})
    assert db.one('SELECT owner FROM sync_locks WHERE project_id=?',(pid,))['owner']==two.run_id
    two.release('completed',{})


def test_gap_decisions_optimistic_version_and_conversation_history(client):
    from app.gap_state import record_gap
    pid=project(client);db=client.app.state.store
    with db.tx() as c:
        gid=record_gap(c,pid,'ENTITY',1,'modeling_choice',{},[],'角色需协商')
    route=f'/api/projects/{pid}/gaps/{gid}/decision'
    response=client.post(route,json={'action':'defer','note':'本轮不确定，稍后讨论','expected_version':0})
    assert response.status_code==200,response.text
    assert response.json()['status']=='deferred'
    stale=client.post(route,json={'action':'resolve','note':'过期决定不可覆盖','expected_version':0})
    assert stale.status_code==409
    reopened=client.post(route,json={'action':'reopen','note':'用户提供更多信息','expected_version':1})
    assert reopened.status_code==200,reopened.text
    resolved=client.post(route,json={'action':'resolve','note':'决定只采用自然人，不建立角色子类','expected_version':2})
    assert resolved.status_code==200,resolved.text
    with db.tx() as c:
        again=record_gap(c,pid,'ENTITY',1,'modeling_choice',{},[],'角色需协商')
    assert again==gid and db.one('SELECT status FROM extraction_gaps WHERE id=?',(gid,))['status']=='resolved'
    assert len(db.many('SELECT * FROM gap_decisions WHERE gap_id=?',(gid,)))==3
    assert any('自然人' in row['content'] for row in db.many('SELECT content FROM messages WHERE project_id=?',(pid,)))
    assert client.get(f'/api/projects/{pid}/gaps?status=resolved').json()['total']==1
    assert client.get(f'/api/projects/{pid}/gaps?status=open').json()['total']==0
    assert db.snapshot(pid)['entities']==[]  # decisions are not auto-approval


def test_gap_targeted_retry_only_reprocesses_one_source_and_preserves_other_cache(client,monkeypatch):
    from app.gap_state import record_gap
    pid=project(client);db=client.app.state.store
    assert upload(client,pid).status_code==200
    client.put(f'/api/projects/{pid}/topic',json={'summary':'模型主题','scope':'设备类型'})
    with db.tx() as c:c.execute('UPDATE documents SET chunks=? WHERE project_id=?',(j([
        {'kind':'prose','section':'定义','locator':'L1','text':'实体：设备'},
        {'kind':'prose','section':'定义','locator':'L2','text':'实体：部件'}]),pid))
    documents=db.many('SELECT * FROM documents WHERE project_id=?',(pid,));pieces=list(incremental.segments(documents))
    cfg=client.app.state.model_settings.effective();calls=[]
    def extractor(p,checkpoint=None,on_checkpoint=None):
        calls.append(p['locator']);name='设备' if p['locator']=='L1' else '部件'
        return {'entities':[{'name':name,'quote':name}],'attributes':[],'relations':[],
                '_complete':True,'_pending':[],'_unresolved':[],'_coverage':{}}
    incremental.cached_clues(db,pieces[0],cfg,extractor)
    calls.clear()
    with db.tx() as c:gid=record_gap(c,pid,'ENTITY',2,'retryable_error',{},[],'部件原处理失败',
        piece=pieces[1],cache_key=incremental.clue_cache_key(pieces[1],cfg))
    monkeypatch.setattr(brain,'joint_extract_segment',extractor)
    request=client.post(f'/api/projects/{pid}/gaps/{gid}/decision',json={
        'action':'retry','note':'修复模型后只重试部件','expected_version':0})
    assert request.status_code==200,request.text
    jid=request.json()['job_id']
    for _ in range(180):
        state=db.one('SELECT state FROM workflow_jobs WHERE id=?',(jid,))['state']
        gap=db.one('SELECT status FROM extraction_gaps WHERE id=?',(gid,))
        if state not in ('queued','running') and gap['status']!='retrying':break
        time.sleep(.02)
    assert state=='succeeded' and gap['status']=='resolved',(state,db.one('SELECT * FROM workflow_jobs WHERE id=?',(jid,)))
    assert calls==['L2']
    assert incremental.cached_clue_status(db,pieces[0],cfg)=='complete'
    assert db.one('SELECT COUNT(*) n FROM proposals WHERE project_id=?',(pid,))['n']==1
    assert db.one('SELECT batch_number FROM extraction_batch_audit WHERE project_id=?',(pid,))['batch_number']==2


def test_gap_targeted_retry_keeps_same_unresolved_question_open(client,monkeypatch):
    from app.gap_state import record_gap
    pid=project(client);db=client.app.state.store
    upload(client,pid);client.put(f'/api/projects/{pid}/topic',json={'summary':'设备','scope':'设备'})
    docs=db.many('SELECT * FROM documents WHERE project_id=?',(pid,));piece=list(incremental.segments(docs))[0]
    cfg=client.app.state.model_settings.effective();reason='来源歧义仍未解决'
    payload={'source_excerpt':'设备','recovery':'awaiting_user'}
    with db.tx() as c:gid=record_gap(c,pid,'ENTITY',1,'discovery',payload,[],reason,
                                    piece=piece,cache_key=incremental.clue_cache_key(piece,cfg))
    def extractor(p,checkpoint=None,on_checkpoint=None):
        return {'entities':[],'attributes':[],'relations':[],'_complete':False,'_pending':[],
                '_state':'awaiting_user','_unresolved':[{'reason':reason,'source_excerpt':'设备'}],'_coverage':{}}
    monkeypatch.setattr(brain,'joint_extract_segment',extractor)
    req=client.post(f'/api/projects/{pid}/gaps/{gid}/decision',json={
        'action':'retry','note':'尝试复核','expected_version':0})
    assert req.status_code==200,req.text
    for _ in range(180):
        gap=db.one('SELECT * FROM extraction_gaps WHERE id=?',(gid,))
        if gap['status']!='retrying':break
        time.sleep(.02)
    assert gap['status']=='open' and gap['decision_revision']==2
    assert len(db.many('SELECT * FROM extraction_gaps WHERE project_id=?',(pid,)))==1


def test_cannot_delete_workspace_during_live_sync(client):
    pid=project(client);db=client.app.state.store
    session=SyncSession(db,pid,'endpoint',B,db.project(pid)['revision'],'test').acquire()
    try:
        r=client.request('DELETE','/api/projects/'+pid,json={
            'confirm_name':db.project(pid)['name'],'expected_revision':db.project(pid)['revision']})
        assert r.status_code==409 and '对账' in r.text
    finally:session.release('closed',{})
    assert db.project(pid) is not None


def test_phase_metrics_count_real_calls_and_available_usage_only(client,monkeypatch):
    from app.metrics import scope
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'{"ok":true}'}}],
                               'usage':{'prompt_tokens':17,'completion_tokens':9,'total_tokens':26}}
    class Model:
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr(brain,'_pooled_http_client',lambda:Model())
    with scope() as metrics,brain.using_model(CONFIG):
        assert brain.query_llm('schema test','synthetic input')['ok']
        client.app.state.store.one('SELECT 1 AS n')
    values=metrics.public()
    assert values['llm_requests']==1
    assert values['prompt_tokens']==17 and values['completion_tokens']==9
    assert values['database_transactions']>=1 and values['sql_statements']>=1
    assert 'synthetic' not in json.dumps(values)


def test_schema_upgrade_is_additive_and_idempotent(client):
    from app.schema_upgrade import upgrade
    pid=project(client);db=client.app.state.store
    with db.tx() as c:
        upgrade(c);upgrade(c)
    assert db.project(pid)['name']=='独立修复验收'
    with db.tx() as c:
        columns={r['name'] for r in c.execute('PRAGMA table_info(workflow_jobs)')}
    assert 'lease_generation' in columns
