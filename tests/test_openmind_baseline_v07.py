"""OpenMind branch-first modeling: real native definitions are the authoritative starting point."""
import importlib
from copy import deepcopy
from uuid import UUID
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.openmind_baseline import model_baseline_context, duplicate_against_baseline
from app.openmind import make_plan
from app import brain

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'
PERSON='20000000-0000-4000-8000-000000000001'
EMPLOYEE='20000000-0000-4000-8000-000000000002'
ORGANIZATION='20000000-0000-4000-8000-000000000003'
NAME='30000000-0000-4000-8000-000000000001'
WORK='40000000-0000-4000-8000-000000000001'


def catalog():
    return {
        'entities':[
            {'id':PERSON,'name':'人员','code':'person','description':'自然人类型','parentId':None,'lockVersion':11,'primaryKeyAttributeIds':[NAME]},
            {'id':EMPLOYEE,'name':'员工','code':'employee','description':'人员子类','parentId':PERSON,'lockVersion':12},
            {'id':ORGANIZATION,'name':'组织','code':'organization','description':'机构组织','parentId':None,'lockVersion':13}
        ],
        'attributes':[{'id':NAME,'name':'姓名','code':'person_name','entityIds':[PERSON],
                       'dataType':'string','lockVersion':21,'dictionaryCode':'dict_name'}],
        'relations':[{'id':WORK,'name':'就职','code':'works_at','sourceEntityIds':[EMPLOYEE],
                      'targetEntityIds':[ORGANIZATION],'executionMode':'SEMANTIC',
                      'steps':'根据劳动合同判断雇佣关系','dependencies':[], 'lockVersion':31}],
        'actions':[], 'capabilities':[]
    }


def make_client(monkeypatch, remote):
    mod=importlib.import_module('app.main')
    writes=[]
    class Fake:
        def __init__(self,*a,**kw):pass
        def catalog(self,branch):assert branch==BRANCH;return deepcopy(remote)
        def graph(self,branch):return {'nodes':[], 'edges':[]}
        def types(self):return {'schema':'test','types':['string']}
        def write(self,branch,step,existing=None):
            item={**step['data'],'lockVersion': (existing or {}).get('lockVersion',0)+1}
            plural={'entity':'entities','attribute':'attributes','relation':'relations'}[step['kind']]
            remote[plural][:]=[r for r in remote[plural] if r['id']!=item['id']]
            remote[plural].append(item)
            writes.append(step)
            return item
        def close(self):pass
    monkeypatch.setattr(mod,'OpenMindClient',Fake)
    return writes


def setup(monkeypatch,tmp_path, remote=None):
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.setenv('OPENMIND_BASE_URL','https://openmind.example')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','test-token')
    remote = remote if remote is not None else catalog()
    writes=make_client(monkeypatch,remote)
    c=TestClient(create_app(tmp_path/'state'))
    # Existing pre-v0.8 local workspace: still allowed to bind after upgrading.
    monkeypatch.delenv('OPENMIND_BASE_URL');monkeypatch.delenv('OPENMIND_ACCESS_TOKEN')
    pid=c.post('/api/projects',json={'name':'基于已有 OpenMind 构建'}).json()['project']['id']
    monkeypatch.setenv('OPENMIND_BASE_URL','https://openmind.example')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','test-token')
    return c,pid,remote,writes


def bind(c,pid):
    r=c.put(f'/api/projects/{pid}/openmind/binding',json={'domain_id':DOMAIN,'branch_id':BRANCH})
    assert r.status_code==200,r.text
    return r.json()


def upload_and_confirm(c,pid):
    r=c.post(f'/api/projects/{pid}/documents',files={
        'file':('doc.txt','实体：人员、员工、组织、项目\n属性：员工.合同编号: string\n关系：员工 worksAt 组织'.encode(),'text/plain')})
    assert r.status_code==200,r.text
    r=c.put(f'/api/projects/{pid}/topic',json={'summary':'关联资料','scope':'在已有人员及组织定义上增加项目和合同编号'})
    assert r.status_code==200,r.text


def test_binding_auto_reads_all_existing_native_shapes(tmp_path,monkeypatch):
    c,pid,remote,_=setup(monkeypatch,tmp_path)
    try:
        result=bind(c,pid)
        base=result['baseline']
        assert base['entity_count']==3 and base['attribute_count']==1 and base['relation_count']==1
        view=c.get(f'/api/projects/{pid}').json()
        assert view['openmind_bound'] and view['openmind_baseline']['branch_id']==BRANCH
        for obj in view['snapshot']['entities']:
            assert obj['origin']=='openmind' and UUID(obj['id'])
        assert len(view['snapshot']['attributes'])==0 # multi-owner native properties are not flattened
        assert len(view['snapshot']['relations'])==0 # executionSpec not flattened
        read=c.get(f'/api/projects/{pid}/openmind/baseline?kind=attribute').json()
        assert read['items'][0]['dictionaryCode']=='dict_name'
        assert read['items'][0]['entityIds']==[PERSON]
        relations=c.get(f'/api/projects/{pid}/openmind/baseline?kind=relation').json()
        assert relations['items'][0]['steps'].startswith('根据劳动合同')
    finally:c.close()


def test_new_attribute_can_bind_existing_remote_entity_but_remote_is_never_rewritten(tmp_path,monkeypatch):
    c,pid,remote,writes=setup(monkeypatch,tmp_path)
    try:
        bind(c,pid)
        upload_and_confirm(c,pid)
        # Same-name class is already present: duplication must be blocked, not queued.
        duplicate=c.post(f'/api/projects/{pid}/proposals',json={
            'kind':'entity','payload':{'name':'人员','kind':'class','description':'重复'}}).json()
        assert not duplicate['created'] and duplicate['reused'] and '已在 OpenMind' in duplicate['reused'][0]
        # Attribute on an inherited parent must be reused, not rebound on descendant.
        repeated=c.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'})
        assert repeated.status_code==200,repeated.text
        duplicate=c.post(f'/api/projects/{pid}/proposals',json={
            'kind':'attribute','payload':{'name':'姓名','owner_id':EMPLOYEE,'datatype':'string'}}).json()
        assert not duplicate['created'] and '父类' in duplicate['reused'][0]
        attr=c.post(f'/api/projects/{pid}/proposals',json={
            'kind':'attribute','payload':{'name':'合同编号','owner_id':EMPLOYEE,'datatype':'string',
                                          'required':False,'multivalued':False,'description':'员工劳动合同编号'}}).json()
        assert len(attr['created'])==1
        assert c.post(f'/api/projects/{pid}/proposals/{attr["created"][0]}/decision',json={'action':'accept'}).status_code==200
        assert c.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}).status_code==200
        new_rel=c.post(f'/api/projects/{pid}/proposals',json={
            'kind':'relation','payload':{'source_id':EMPLOYEE,'target_id':ORGANIZATION,
              'predicate':'worksAt','description':'根据合同判断'}}).json()
        # OpenMind relation is already defined; do not create duplicate endpoints/semantics.
        assert not new_rel['created'] and new_rel['reused']
        assert c.post(f'/api/projects/{pid}/stage',json={'target':'REVIEW'}).status_code==200
        release=c.post(f'/api/projects/{pid}/publish',json={'note':'只增加员工合同编号'}).json()
        plan=c.get(f'/api/projects/{pid}/openmind/plan?remote=true').json()
        assert plan['ready'],plan['blockers']
        assert plan['counts']=={'entity':0,'attribute':1,'relation':0}
        assert plan['steps'][0]['data']['entityIds']==[EMPLOYEE]
        assert not writes
        sync=c.post(f'/api/projects/{pid}/openmind/sync',json={
            'expected_revision':release['revision'],'confirmation':'CONFIRM_OPENMIND_WRITE'})
        assert sync.status_code==200,sync.text
        assert len(writes)==1 and writes[0]['kind']=='attribute'
        assert len(remote['entities'])==3 and len(remote['relations'])==1
    finally:c.close()


def test_remote_change_rejects_stale_proposals_and_blocks_stale_sync(tmp_path,monkeypatch):
    c,pid,remote,_=setup(monkeypatch,tmp_path)
    try:
        bind(c,pid);upload_and_confirm(c,pid)
        q=c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'项目','kind':'class'}}).json()
        assert q['created']
        previous=c.get(f'/api/projects/{pid}').json()['project']['revision']
        remote['attributes'][0]['description']='external change'
        changed=c.post(f'/api/projects/{pid}/openmind/baseline/refresh')
        assert changed.status_code==200,changed.text
        assert changed.json()['baseline']['changed']
        state=c.get(f'/api/projects/{pid}').json()
        assert state['project']['revision']==previous+1
        assert any(x['status']=='rejected' and x['id']==q['created'][0] for x in state['proposals'])
        # A second run with identical catalog does not increment local revision.
        changed_again=c.post(f'/api/projects/{pid}/openmind/baseline/refresh')
        assert not changed_again.json()['baseline']['changed']
        assert c.get(f'/api/projects/{pid}').json()['project']['revision']==previous+1
    finally:c.close()


def test_bound_without_baseline_must_not_continue_as_document_only(tmp_path,monkeypatch):
    c,pid,_,_=setup(monkeypatch,tmp_path)
    try:
        # Simulate a v0.6 migrated project that stored the binding without a fetched catalog.
        from app.store import now
        with c.app.state.store.tx() as sql:
            sql.execute('INSERT INTO openmind_bindings VALUES(?,?,?,?)',(pid,DOMAIN,BRANCH,now()))
        assert c.post(f'/api/projects/{pid}/documents',files={'file':('x.txt',b'hello','text/plain')}).status_code==200
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=summary')
        assert r.status_code==409 and '先' in r.text
        assert c.put(f'/api/projects/{pid}/topic',json={'summary':'s','scope':'t'}).status_code==409
        assert c.post(f'/api/projects/{pid}/openmind/baseline/refresh').status_code==200
        assert c.put(f'/api/projects/{pid}/topic',json={'summary':'s','scope':'t'}).status_code==200
    finally:c.close()


def test_existing_remote_entity_is_read_only_and_cannot_be_deleted(tmp_path,monkeypatch):
    c,pid,remote,_=setup(monkeypatch,tmp_path)
    try:
        bind(c,pid);upload_and_confirm(c,pid)
        r=c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{
            'operation':'delete','target_id':PERSON}})
        assert r.status_code==422 and '只读' in r.text
    finally:c.close()


def test_model_retrieval_sends_native_branch_context_to_candidate_llm(monkeypatch):
    remote=catalog()
    doc={'id':'d','filename':'a.txt','chunks':'[{"locator":"L1","text":"员工合同编号与组织的关系"}]'}
    called=[]
    def fake_query(system,user):
        called.append((system,user))
        return {'proposals':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',fake_query)
    with brain.using_model({'base_url':'https://fake.example/v1','model':'test','api_key':''}):
        result=brain.proposal_batches('PROPERTY',[doc],{'entities':[], 'attributes':[], 'relations':[]},
                                       '复用已有实体',max_batches=1,baseline=remote)
    assert result['complete']
    assert 'existing_openmind_branch' in called[0][1]
    assert EMPLOYEE in called[0][1] and PERSON in called[0][1]
    assert 'person_name' in called[0][1]


def test_retrieval_context_is_bounded_and_reports_omissions():
    remote=catalog()
    remote['entities']=[{'id':str(__import__('uuid').uuid4()),'name':'实体'+str(i),
                         'code':'entity_'+str(i),'description':'大型业务定义'+'内容'*100} for i in range(600)]
    ctx=model_baseline_context(remote,'ENTITY','实体599',limit=2500)
    assert ctx['totals']['entities']==600
    assert ctx['selected']['entities'] < 600
    assert ctx['omitted']['entities']==600-ctx['selected']['entities']
    assert len(str(ctx)) < 3000


def test_remote_catalog_changes_after_release_prevent_sync(tmp_path,monkeypatch):
    c,pid,remote,writes=setup(monkeypatch,tmp_path)
    try:
        bind(c,pid);upload_and_confirm(c,pid)
        assert c.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}).status_code==200
        assert c.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}).status_code==200
        assert c.post(f'/api/projects/{pid}/stage',json={'target':'REVIEW'}).status_code==200
        release=c.post(f'/api/projects/{pid}/publish',json={'note':'在远端基线上发布'}).json()
        remote['entities'][0]['description']='别的用户已经更改人员定义'
        r=c.post(f'/api/projects/{pid}/openmind/sync',json={'expected_revision':release['revision'],
            'confirmation':'CONFIRM_OPENMIND_WRITE'})
        assert r.status_code==409 and '发生变化' in r.text
        assert writes==[]
    finally:c.close()


def test_changing_openmind_server_invalidates_cached_branch(tmp_path,monkeypatch):
    c,pid,remote,_=setup(monkeypatch,tmp_path)
    try:
        bind(c,pid)
        assert c.get(f'/api/projects/{pid}').json()['openmind_baseline']
        monkeypatch.setenv('OPENMIND_BASE_URL','https://different-server.example')
        assert c.get(f'/api/projects/{pid}').json()['openmind_baseline'] is None
        r=c.get(f'/api/projects/{pid}/openmind/baseline?kind=entity')
        assert r.status_code==409 and '刷新分支基线' in r.text
    finally:c.close()


def test_broken_remote_catalog_cannot_be_silently_filtered():
    from app.openmind_baseline import canonical_catalog
    from app.openmind import OpenMindError
    remote=catalog()
    remote['entities'].append({'name':'丢失主键的远端实体','code':'missing_id'})
    with pytest.raises(OpenMindError,match='UUID'):
        canonical_catalog(remote)
    remote=catalog();remote['relations'][0]['targetEntityIds']=[str(__import__('uuid').uuid4())]
    with pytest.raises(OpenMindError,match='分支外'):
        canonical_catalog(remote)
