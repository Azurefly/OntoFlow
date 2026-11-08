"""Regression tests for native OpenMind mapping and review-before-write."""
import importlib, json, os
from copy import deepcopy
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.openmind import make_plan,OpenMindClient,OpenMindError

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'

def sample():
    return {'project':{'revision':8},'entities':[
      {'id':'p','name':'人员','kind':'class','description':'自然人','class_id':None},
      {'id':'e','name':'员工','kind':'class','description':'人员子类','class_id':None},
      {'id':'o','name':'组织','kind':'class','description':'单位','class_id':None}],
      'attributes':[{'id':'a','name':'姓名','owner_id':'p','datatype':'string','multivalued':0,'required':0,'description':'姓名'}],
      'relations':[{'id':'inherit','source_id':'e','target_id':'p','predicate':'subClassOf','description':'员工继承人员'},
                   {'id':'work','source_id':'e','target_id':'o','predicate':'worksAt','description':'只有在工作雇佣记录证明人员属于该组织时，关系才成立'}]}

def test_make_plan_native_fields_and_inheritance():
    p=make_plan(sample(),'local1',DOMAIN,BRANCH)
    assert p['ready'],p['blockers']
    assert [s['kind'] for s in p['steps']]==['entity','entity','entity','attribute','relation']
    byname={s['data']['name']:s for s in p['steps']}
    assert byname['员工']['data']['parentId']==byname['人员']['data']['id']
    assert byname['姓名']['data']['entityIds']==[byname['人员']['data']['id']]
    assert 'attributeIds' not in byname['员工']['data']
    assert byname['任职于']['data']['sourceEntityIds']==[byname['员工']['data']['id']]
    assert byname['任职于']['data']['executionMode']=='SEMANTIC'
    assert byname['任职于']['data']['steps'].startswith('只有在工作')
    assert all(len(s['data']['id'])==36 for s in p['steps'])
    assert p==make_plan(sample(),'local1',DOMAIN,BRANCH)

@pytest.mark.parametrize('change', ['instance','multivalued','decimal','no-steps','multi-parent','missing-map'])
def test_unsupported_contract_semantics_are_blocked(change):
    snap=sample()
    mapped={}
    if change=='instance':snap['entities'][0]['kind']='instance'
    if change=='multivalued':snap['attributes'][0]['multivalued']=1
    if change=='decimal':snap['attributes'][0]['datatype']='decimal'
    if change=='no-steps':snap['relations'][1]['description']=''
    if change=='multi-parent':snap['relations'].append({'id':'inherit2','source_id':'e','target_id':'o','predicate':'subClassOf','description':'多继承'})
    if change=='missing-map':mapped={('entity','deleted'):{'remote_id':DOMAIN,'code':'old'}}
    plan=make_plan(snap,'local1',DOMAIN,BRANCH,mappings=mapped)
    assert not plan['ready'],change
    assert plan['blockers']


def test_catalog_detects_collision_and_requires_lock():
    p=make_plan(sample(),'local1',DOMAIN,BRANCH)
    obj=p['steps'][0]['data']
    catalog={'entities':[{'id':'other-uuid','code':obj['code']}],'attributes':[],'relations':[],'actions':[],'capabilities':[]}
    assert not make_plan(sample(),'local1',DOMAIN,BRANCH,catalog=catalog)['ready']
    catalog['entities']=[{'id':obj['id'],'code':obj['code'],'lockVersion':12}]
    plan=make_plan(sample(),'local1',DOMAIN,BRANCH,catalog=catalog)
    assert plan['ready'];assert plan['steps'][0]['action']=='update'
    catalog['entities'][0].pop('lockVersion')
    assert not make_plan(sample(),'local1',DOMAIN,BRANCH,catalog=catalog)['ready']


def test_update_preserves_openmind_owned_fields(monkeypatch):
    captured=[]
    def handler(req):
        captured.append(json.loads(req.content));return httpx.Response(200,json={'id':'123'})
    with_client=OpenMindClient('https://openmind.example','token',transport=httpx.MockTransport(handler))
    try:
        p=make_plan(sample(),'local1',DOMAIN,BRANCH)
        entity=p['steps'][0]
        existing={**entity['data'],'primaryKeyAttributeIds':['remote-pk'], 'displayAttributeIds':['remote-display'],
                  'attributeAliases':{'remote-pk':'人员编号'},'fetchLogic':'maintained by OpenMind','lockVersion':123}
        with_client.write(BRANCH,entity,existing)
        body=captured[0]
        assert body['lockVersion']==123
        assert body['data']['primaryKeyAttributeIds']==['remote-pk']
        assert body['data']['attributeAliases']=={'remote-pk':'人员编号'}
        assert body['data']['fetchLogic']=='maintained by OpenMind'
    finally:with_client.close()


def test_http_bound_plan_and_explicit_sync(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('OPENMIND_BASE_URL','https://openmind.example')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','secret-api-token')
    mod=importlib.import_module('app.main')
    remote={'entities':[], 'attributes':[], 'relations':[], 'actions':[], 'capabilities':[]}
    calls=[]
    class FakeClient:
        def __init__(self,*args,**kwargs):pass
        def catalog(self,branch):assert branch==BRANCH;return deepcopy(remote)
        def types(self):return {'schema':'demo','types':['string']}
        def domains(self):return {'items':[],'total':0}
        def graph(self,branch):return {'nodes':[],'edges':[]}
        def write(self,branch,step,existing=None):
            kind=step['kind'];obj={**step['data'],'lockVersion':(existing['lockVersion']+1 if existing else 1)}
            target=remote[{'entity':'entities','attribute':'attributes','relation':'relations'}[kind]]
            target[:]=[r for r in target if r['id']!=obj['id']]
            target.append(obj)
            calls.append((kind, 'put' if existing else 'post'))
            return obj
        def close(self):pass
    monkeypatch.setattr(mod,'OpenMindClient',FakeClient)
    with TestClient(create_app(tmp_path/'state')) as client:
        # v0.7 migration compatibility: project existed before OpenMind was configured.
        monkeypatch.delenv('OPENMIND_BASE_URL');monkeypatch.delenv('OPENMIND_ACCESS_TOKEN')
        pid=client.post('/api/projects',json={'name':'OpenMind API 闭环'}).json()['project']['id']
        monkeypatch.setenv('OPENMIND_BASE_URL','https://openmind.example')
        monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','secret-api-token')
        assert client.put(f'/api/projects/{pid}/openmind/binding',json={'domain_id':'bad','branch_id':BRANCH}).status_code==422
        bound=client.put(f'/api/projects/{pid}/openmind/binding',json={'domain_id':DOMAIN,'branch_id':BRANCH})
        assert bound.status_code==200,bound.text
        assert client.get(f'/api/projects/{pid}/openmind/plan').status_code==409 # cannot write unreviewed drafts
        assert client.post(f'/api/projects/{pid}/openmind/test').json()['connected']
        assert client.post(f'/api/projects/{pid}/openmind/sync',json={'expected_revision':0,'confirmation':'NO'}).status_code==400
        content='实体：网关\n构成设备结构'.encode()
        assert client.post(f'/api/projects/{pid}/documents',files={'file':('a.txt',content,'text/plain')}).status_code==200
        assert client.put(f'/api/projects/{pid}/topic',json={'summary':'设备结构','scope':'只建模网关类型'}).status_code==200
        q=client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'网关','kind':'class','description':'网关类别'}}).json()['created'][0]
        assert client.post(f'/api/projects/{pid}/proposals/{q}/decision',json={'action':'accept'}).status_code==200
        for stage in ('PROPERTY','RELATION','REVIEW'):
            assert client.post(f'/api/projects/{pid}/stage',json={'target':stage}).status_code==200
        release=client.post(f'/api/projects/{pid}/publish',json={'note':'人工完成审核'}).json()
        plan=client.get(f'/api/projects/{pid}/openmind/plan?remote=true').json()
        assert plan['ready'] and plan['counts']['entity']==1
        assert not calls
        assert client.post(f'/api/projects/{pid}/openmind/sync',json={'expected_revision':release['revision']-1,'confirmation':'CONFIRM_OPENMIND_WRITE'}).status_code==409
        result=client.post(f'/api/projects/{pid}/openmind/sync',json={'expected_revision':release['revision'],'confirmation':'CONFIRM_OPENMIND_WRITE'})
        assert result.status_code==200,result.text
        assert result.json()['status']=='definitions_saved'
        assert calls==[('entity','post')]
        # Identical reviewed content is reconciled and reused without redundant POST/PUT.
        result=client.post(f'/api/projects/{pid}/openmind/sync',json={'expected_revision':release['revision'],'confirmation':'CONFIRM_OPENMIND_WRITE'})
        assert result.status_code==200,result.text
        assert calls==[('entity','post')]
        assert result.json()['http_writes']==0
        assert client.get(f'/api/projects/{pid}/openmind/status').json()['mappings']
