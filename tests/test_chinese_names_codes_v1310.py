"""Business-facing Chinese names and separately persisted native ASCII codes.

Tests use actual API/SQLite transactions and the real OpenMind plan builder.
They do not mutate a live OpenMind branch or simulate PG as production evidence.
"""
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.naming import (prepare_payload,relation_name,code_error,generated_code,
                        assignment_error,is_chinese_name)
from app.openmind import make_plan
from app.store import Store,now,uid
from app.ontology import build_graph
from rdflib.namespace import RDFS

DOMAIN=str(uuid.uuid4()); BRANCH=str(uuid.uuid4())

@pytest.fixture
def env(tmp_path):
    with TestClient(create_app(tmp_path/'data')) as client:
        p=client.post('/api/projects',json={'name':'中文名称与英文编码'}).json()['project']['id']
        assert client.post(f'/api/projects/{p}/documents',files={'file':('test.txt','实体：用户、终端'.encode(),'text/plain')}).status_code==200
        assert client.put(f'/api/projects/{p}/topic',json={'summary':'对象类型','scope':'只构建类型和定义'}).status_code==200
        yield client,p


def propose(c,p,k,payload):
    response=c.post(f'/api/projects/{p}/proposals',json={'kind':k,'payload':payload})
    assert response.status_code==200,response.text
    assert response.json()['created'],response.json()
    return response.json()['created'][0]


def accept(c,p,q,payload=None,expected=200):
    request={'action':'accept'}
    if payload is not None:request['payload']=payload
    r=c.post(f'/api/projects/{p}/proposals/{q}/decision',json=request)
    assert r.status_code==expected,r.text
    return r


def entities(c,p):
    for name,code in [('用户','user_type'),('终端','terminal')]:
        accept(c,p,propose(c,p,'entity',{'name':name,'code':code,'kind':'class','description':name+'类型'}))
    return {e['name']:e['id'] for e in c.get(f'/api/projects/{p}').json()['snapshot']['entities']}


def relation(c,p):
    ids=entities(c,p)
    assert c.post(f'/api/projects/{p}/stage',json={'target':'PROPERTY'}).status_code==200
    q=propose(c,p,'attribute',{'owner_id':ids['用户'],'name':'创建时间','code':'created_at','datatype':'date'})
    accept(c,p,q)
    assert c.post(f'/api/projects/{p}/stage',json={'target':'RELATION'}).status_code==200
    q=propose(c,p,'relation',{'source_id':ids['用户'],'target_id':ids['终端'],
             'name':'拥有终端','code':'owns_terminal','description':'以已确认的用户终端绑定为依据'})
    return accept(c,p,q).json()['snapshot']


def test_api_three_kinds_persist_names_codes_and_write_plan(env):
    c,p=env
    snap=relation(c,p)
    assert snap['entities'][0]['code']
    assert snap['attributes'][0]['name']=='创建时间' and snap['attributes'][0]['code']=='created_at'
    assert snap['relations'][0]['name']=='拥有终端' and snap['relations'][0]['code']=='owns_terminal'
    reopened=c.get(f'/api/projects/{p}').json()['snapshot']
    assert reopened==snap
    plan=make_plan(reopened,p,DOMAIN,BRANCH)
    assert plan['ready'],plan['blockers']
    rows={s['data']['code']:s['data'] for s in plan['steps']}
    assert rows['user_type']['name']=='用户'
    assert rows['created_at']['name']=='创建时间'
    assert rows['owns_terminal']['name']=='拥有终端'
    assert all(is_chinese_name(x['name']) and re.fullmatch(r'[a-z][a-z0-9_]*',x['code']) for x in rows.values())


def test_rename_does_not_change_code_uuid_or_predicate(env):
    c,p=env
    snap=relation(c,p);old=snap['relations'][0]
    q=propose(c,p,'relation',{**old,'operation':'update','record_id':old['id'],'name':'持有终端'})
    got=accept(c,p,q).json()['snapshot']['relations'][0]
    assert (got['id'],got['code'],got['predicate'])==(old['id'],old['code'],old['predicate'])
    assert got['name']=='持有终端'


def test_english_only_candidate_stays_pending_until_user_adds_chinese(env):
    c,p=env
    q=propose(c,p,'entity',{'name':'User','code':'user_type'})
    fail=accept(c,p,q,expected=422)
    assert '中文名称' in fail.text
    assert c.get(f'/api/projects/{p}/proposals/page').json()['total']==1
    accepted=accept(c,p,q,{'name':'用户','code':'user_type','kind':'class'})
    assert accepted.json()['snapshot']['entities'][0]['name']=='用户'


@pytest.mark.parametrize('code',['中文编码','Code','has-Part','a.b','1start','class','a'*65])
def test_invalid_english_relation_code_rejected(code):
    assert code_error('relation',code)


@pytest.mark.parametrize('code',['rel_01','owns_terminal','x2','a'*64])
def test_valid_english_relation_code(code):
    assert code_error('relation',code)==''


def test_unknown_relation_code_does_not_become_chinese_name(env):
    c,p=env
    ids=entities(c,p)
    c.post(f'/api/projects/{p}/stage',json={'target':'PROPERTY'})
    c.post(f'/api/projects/{p}/stage',json={'target':'RELATION'})
    q=propose(c,p,'relation',{'source_id':ids['用户'],'target_id':ids['终端'],
                             'predicate':'opaque_relation','code':'opaque_relation','description':'需要用户命名'})
    assert '中文名称' in accept(c,p,q,expected=422).text
    row=c.get(f'/api/projects/{p}/proposals/page?kind=relation').json()['items'][0]
    assert row['payload']['name']==''


def test_legacy_verified_relation_name_recovered_without_modifying_identifier():
    old={'predicate':'rel_01','description':'说明','evidence':json.dumps([
        {'verified':True,'document_id':'doc','locator':'关系定义','quote':'| 关系 | 拥有终端 |'}],ensure_ascii=False)}
    assert relation_name(old)=='拥有终端'
    assert old['predicate']=='rel_01' and 'name' not in old
    assert relation_name({'predicate':'unknown_opaque'})==''


def test_no_unverified_provenance_is_used_for_label():
    assert relation_name({'predicate':'opaque','evidence':[{'verified':False,'source_name':'伪造名称'}]})==''


def test_code_collision_preserves_later_candidate(env):
    c,p=env
    accept(c,p,propose(c,p,'entity',{'name':'用户','code':'same_code'}))
    q=propose(c,p,'entity',{'name':'终端','code':'same_code'})
    r=accept(c,p,q,expected=409)
    assert 'CODE' in r.text
    assert len(c.get(f'/api/projects/{p}').json()['snapshot']['entities'])==1


def test_publish_and_restore_preserve_relation_label_and_code(env):
    c,p=env
    snap=relation(c,p);r=snap['relations'][0]
    c.post(f'/api/projects/{p}/stage',json={'target':'REVIEW'})
    published=c.post(f'/api/projects/{p}/publish',json={'note':'中文命名已确认'})
    assert published.status_code==200,published.text
    c.post(f'/api/projects/{p}/stage',json={'target':'RELATION'})
    q=propose(c,p,'relation',{**r,'operation':'update','record_id':r['id'],'name':'分配终端'})
    accept(c,p,q)
    restored=c.post(f'/api/projects/{p}/releases/1/restore')
    assert restored.status_code==200,restored.text
    assert restored.json()['snapshot']['relations'][0]['name']=='拥有终端'
    assert restored.json()['snapshot']['relations'][0]['code']=='owns_terminal'


def test_new_metadata_removed_with_project(env):
    c,p=env
    relation(c,p)
    project=c.get(f'/api/projects/{p}').json()['project']
    r=c.request('DELETE',f'/api/projects/{p}',json={'confirm_name':project['name'],'expected_revision':project['revision']})
    assert r.status_code==200,r.text
    assert c.app.state.store.one('SELECT COUNT(*) n FROM relation_labels WHERE project_id=?',(p,))['n']==0


def test_relation_graph_export_labels_are_chinese(env):
    c,p=env
    snap=relation(c,p)
    graph=build_graph(snap)
    assert '拥有终端' in {str(x) for x in graph.objects(None,RDFS.label)}


def test_mapped_code_is_not_renamed_by_candidate(env):
    c,p=env
    snap=relation(c,p);obj=snap['relations'][0]
    with c.app.state.store.tx() as sql:
        sql.execute('INSERT INTO openmind_mappings VALUES(?,?,?,?,?,?,?)',
                    (p,'relation',obj['id'],str(uuid.uuid4()),obj['code'],1,now()))
    q=propose(c,p,'relation',{**obj,'operation':'update','record_id':obj['id'],'name':'持有终端','code':'new_code'})
    assert '已同步' in accept(c,p,q,expected=409).text
    assert c.get(f'/api/projects/{p}').json()['snapshot']['relations'][0]['code']==obj['code']


def test_legacy_schema_install_is_additive(tmp_path):
    root=tmp_path/'legacy'
    store=Store(root)
    with store.tx() as c:
        c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',('p','旧项目',now(),now()))
        c.execute('DROP TABLE relation_labels')
    reopened=Store(root)
    assert reopened.project('p')['name']=='旧项目'
    assert reopened.snapshot('p')['relations']==[]
    assert reopened.one("SELECT name FROM sqlite_master WHERE name='relation_labels'")


def test_native_unknown_label_blocks_write_plan_instead_of_using_code():
    snap={'project':{'revision':1},'entities':[
        {'id':'one','name':'甲类','kind':'class','description':''},
        {'id':'two','name':'乙类','kind':'class','description':''}],
        'attributes':[],'relations':[{'id':'r','source_id':'one','target_id':'two',
                        'predicate':'unknown_code','description':'有业务依据'}]}
    result=make_plan(snap,'local',DOMAIN,BRANCH)
    assert not result['ready'] and any('中文名称' in v for v in result['blockers'])


def test_relation_label_kept_during_incremental_alignment():
    from app.incremental import align_clues
    p={'document_id':'d','locator':'表格','content':'| 关系 | 拥有终端 |'}
    current={'entities':[{'id':'a','name':'用户','kind':'class'},{'id':'b','name':'终端','kind':'class'}], 'attributes':[],'relations':[]}
    clues={'relations':[{'source':'用户','predicate':'rel_01','code':'rel_01','label':'拥有终端',
                         'target':'终端','quote':p['content']}]}
    candidates,gaps=align_clues('RELATION',clues,current,p)
    assert not gaps
    assert candidates[0]['payload']['name']=='拥有终端'
    assert candidates[0]['payload']['code']=='rel_01'
