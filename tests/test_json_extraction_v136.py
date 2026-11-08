"""JSON/ZIP extraction contract: no instance values, trusted identity, explicit gaps."""
from __future__ import annotations
import io,json,zipfile
from copy import deepcopy
import os
import pytest
from pathlib import Path
from uuid import UUID
import pytest
from fastapi.testclient import TestClient

from app import brain,incremental
from app.extraction import extract,ExtractionError,SUPPORTED
from app.json_discovery import JSONDiscoveryError,extract_json_obj,extract_zip
from app.openmind_baseline import canonical_catalog
from app.openmind import OpenMindError

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'
E1='20000000-0000-4000-8000-000000000001'
E2='20000000-0000-4000-8000-000000000002'
A1='30000000-0000-4000-8000-000000000001'
A2='30000000-0000-4000-8000-000000000002'
R1='40000000-0000-4000-8000-000000000001'

def sample_native():
    return {'schemaVersion':13,'domain':{'id':DOMAIN,'name':'综合业务本体','activeBranchId':BRANCH},
        'entities':[
          {'id':E1,'branchId':BRANCH,'domainId':DOMAIN,'code':'person','name':'自然人','parentId':None},
          {'id':E2,'branchId':BRANCH,'domainId':DOMAIN,'code':'reporter','name':'报警人','parentId':E1}],
        'attributes':[
          {'id':A1,'branchId':BRANCH,'domainId':DOMAIN,'code':'created_at','name':'创建时间','dataType':'date','entityIds':[E2]},
          {'id':A2,'branchId':BRANCH,'domainId':DOMAIN,'code':'standalone','name':'独立标识','dataType':'string','entityIds':[]}],
        'relations':[{'id':R1,'branchId':BRANCH,'domainId':DOMAIN,'code':'reports','name':'实施报警','sourceEntityIds':[E2],
                      'targetEntityIds':[E1],'description':'报警人报告给自然人','executionMode':'SEMANTIC','steps':'举报对象关系核验'}],
        'actions':[],'capabilities':[]}


def make_zip(path,data=None,other=None):
    data=data or sample_native()
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('manifest.json',json.dumps({'schemaVersion':data['schemaVersion']}))
        z.writestr('data.json',json.dumps(data,ensure_ascii=False))
        if other:
            for key,value in other.items():z.writestr(key,value)


def collect(path,baseline=None):
    doc=extract(path);records={'id':'doc','project_id':'p','filename':path.name,'chunks':json.dumps(doc['chunks'])}
    summary=brain.analyze([records],baseline=baseline)
    output={};clues={'entities':[],'attributes':[],'relations':[]};gaps=[]
    for piece in incremental.segments([records]):
        out=brain.joint_extract_segment(piece)
        for key in clues:clues[key]+=out[key]
        gaps.extend(out['_unresolved'])
        for stage in ('ENTITY','PROPERTY','RELATION'):
            output.setdefault(stage,[])
            candidates,unresolved=incremental.align_clues(stage,out,
                    {'entities':[],'attributes':[],'relations':[]},piece,baseline=baseline)
            output[stage].extend(candidates)
            gaps.extend(unresolved)
    return doc,summary,clues,output,gaps


def test_native_json_and_zip_have_same_typed_extraction(tmp_path,monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *x,**kw:(_ for _ in ()).throw(AssertionError('JSON must not call LLM')))
    obj=sample_native()
    js=tmp_path/'source.json';js.write_text(json.dumps(obj,ensure_ascii=False))
    z=tmp_path/'source.zip';make_zip(z)
    for path in (js,z):
        parsed,summary,clues,out,gaps=collect(path)
        assert len(parsed['chunks'])==7  # 2 entity, 1 binding, 1 inherited edge, 1 relation, 1 unbound, 1 overview
        assert summary['coverage']['strategy']=='json-schema-evidence'
        assert summary['coverage']['schema_definitions']['attributes']==2
        assert 'JSON 行' not in summary['summary']
        assert [x['name'] for x in clues['entities']]==['自然人','报警人']
        assert [x['name'] for x in clues['attributes']]==['创建时间']
        assert {r['predicate'] for r in clues['relations']}=={'subClassOf','reports'}
        assert any('未绑定' in x['reason'] for x in gaps)
        assert len(out['ENTITY'])==2
        assert len(out['PROPERTY'])==0  # cannot map without approved classes
        assert len(out['RELATION'])==0


def test_same_branch_is_read_only_and_skips_all_candidates(tmp_path):
    p=tmp_path/'native.zip';make_zip(p)
    b=canonical_catalog(sample_native())
    doc,summary,clues,out,gaps=collect(p,baseline=b)
    assert all(not out[stage] for stage in out)
    assert any('未绑定' in x['reason'] for x in gaps)
    assert len(doc['chunks'])<20


def test_source_branch_uuid_never_reused_for_different_branch(tmp_path):
    p=tmp_path/'native.zip';make_zip(p)
    catalog=deepcopy(sample_native())
    target='10000000-0000-4000-8000-000000000003'
    for group in ('entities','attributes','relations'):
        for obj in catalog[group]:obj['branchId']=target
    b=canonical_catalog(catalog)
    _,_,_,out,_=collect(p,baseline=b)
    # Same UUID in a different branch is NOT an automatic identity match.
    assert out['ENTITY']


def test_cross_kind_code_collision_is_rejected():
    data=sample_native();data['relations'][0]['code']='person'
    with pytest.raises(OpenMindError,match='CODE 冲突'):
        canonical_catalog(data)


def test_zip_rejects_zip_slip_and_unknown_files(tmp_path):
    p=tmp_path/'bad.zip';make_zip(p,other={'../evil.txt':'payload'})
    with pytest.raises(ExtractionError,match='非法路径'):
        extract(p)
    make_zip(p,other={'README.sh':'echo bad'})
    with pytest.raises(ExtractionError,match='未知文件'):
        extract(p)


def test_generic_json_returns_types_not_instance_values(tmp_path):
    p=tmp_path/'records.json'
    p.write_text(json.dumps({'警情记录':[{'姓名':'李某','描述':'李某报警。','金额':500,'已受理':True},
                           {'姓名':'张某','描述':'张某报警。','金额':1000,'已受理':False}]},ensure_ascii=False))
    parsed,summary,clues,_,_=collect(p)
    assert len(clues['entities'])>=1
    assert {'警情记录'} <= {x['name'] for x in clues['entities']}
    assert not {'张某','李某','500','1000'} & {x['name'] for x in clues['entities']}
    assert any(x['name']=='金额' and x['datatype']=='integer' for x in clues['attributes'])
    assert '1000' not in summary['summary']


def test_api_upload_zip_then_summary(tmp_path,monkeypatch):
    from app.main import create_app
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    p=tmp_path/'source.zip';make_zip(p)
    with TestClient(create_app(tmp_path/'workspace',testing=True)) as c:
        res=c.post('/api/projects',json={'name':'JSON 本体测试'})
        assert res.status_code==200,res.text
        pid=res.json()['project']['id']
        uploaded=c.post(f'/api/projects/{pid}/documents',files={'file':('source.zip',p.read_bytes(),'application/zip')})
        assert uploaded.status_code==200,uploaded.text
        assert uploaded.json()['chunks']>0
        response=c.post(f'/api/projects/{pid}/analyze')
        assert response.status_code==200,response.text
        assert response.json()['coverage']['schema_definitions']['entities']==2
        assert '原生本体导出' in response.json()['summary']
        d=c.get(f'/api/projects/{pid}/documents/{uploaded.json()["document"]["id"]}/chunks')
        assert d.status_code==200
        assert any(x['kind']=='native-attribute' for x in d.json()['chunks'])


def test_original_domain_export_when_available(monkeypatch,tmp_path):
    source=Path(os.getenv('ONTOFLOW_JSON_FIXTURE','/mnt/data/key_person_multidimensional_behavior_analysis_v3.zip'))
    if not source.exists():pytest.skip('Set ONTOFLOW_JSON_FIXTURE to run the real native export test')
    monkeypatch.setattr(brain,'query_llm',lambda *x,**kw:(_ for _ in ()).throw(AssertionError('LLM unexpectedly invoked')))
    with zipfile.ZipFile(source) as z:original=json.loads(z.read('data.json'))
    baseline=canonical_catalog(original)
    parsed,summary,clues,out,gaps=collect(source,baseline=baseline)
    assert summary['coverage']['schema_definitions']=={k:len(baseline[k]) for k in ('actions','attributes','capabilities','entities','relations')}
    assert len(clues['entities'])==len(baseline['entities'])
    assert len(clues['attributes'])==sum(len(a.get('entityIds') or []) for a in baseline['attributes'])
    assert len([x for x in clues['relations'] if x['predicate']!='subClassOf'])==len(baseline['relations'])
    assert not any(out.values())
    assert any('未绑定' in x['reason'] for x in gaps)
    assert all(x['kind']!='prose' for x in parsed['chunks'])


def test_api_json_proposals_approve_and_preserve_shared_code(tmp_path,monkeypatch):
    """Source-code proof is recomputed by server for each evidence locator."""
    from app.main import create_app
    from app.openmind import make_plan
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    data=sample_native()
    data['entities'][1]['parentId']=None  # independent owners can share one attribute
    data['attributes']=data['attributes'][:1]
    data['attributes'][0]['entityIds']=[E1,E2]
    with TestClient(create_app(tmp_path/'workspace',testing=True)) as client:
        def ok(r):
            assert r.status_code==200,r.text
            return r.json()
        pid=ok(client.post('/api/projects',json={'name':'JSON共享属性审核'}))['project']['id']
        ok(client.post(f'/api/projects/{pid}/documents',files={'file':('ontology.json',json.dumps(data).encode(),'application/json')}))
        overview=ok(client.post(f'/api/projects/{pid}/analyze'))
        ok(client.put(f'/api/projects/{pid}/topic',json={'summary':overview['summary'],'scope':'实体属性及关系建模'}))
        result=ok(client.post(f'/api/projects/{pid}/propose'))
        assert len(result.get('created',[]))==2,result
        for prop in result['created']:
            ok(client.post(f'/api/projects/{pid}/proposals/{prop}/decision',json={'action':'accept'}))
        ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
        result=ok(client.post(f'/api/projects/{pid}/propose'))
        assert len(result.get('created',[]))==2,result
        for prop in result['created']:
            ok(client.post(f'/api/projects/{pid}/proposals/{prop}/decision',json={'action':'accept'}))
        snapshot=ok(client.get(f'/api/projects/{pid}'))['snapshot']
        assert len(snapshot['attributes'])==2
        assert all(json.loads(a['evidence'])[0]['source_code']=='created_at' for a in snapshot['attributes'])
        plan=make_plan(snapshot,pid,DOMAIN,BRANCH)
        assert plan['ready'],plan['blockers']
        typed=[x['data'] for x in plan['steps'] if x['kind']=='attribute']
        assert len(typed)==1
        assert typed[0]['code']=='created_at' and len(typed[0]['entityIds'])==2
        classes=[x['data'] for x in plan['steps'] if x['kind']=='entity']
        assert {x['code'] for x in classes}=={'person','reporter'}


def test_jsonl_and_explicit_ontology_json(tmp_path):
    path=tmp_path/'records.jsonl'
    path.write_text('\n'.join([json.dumps({'姓名':'张某','年龄':32},ensure_ascii=False),
                              json.dumps({'姓名':'李某','年龄':28},ensure_ascii=False)]))
    parsed=extract(path)
    assert any(c['kind']=='json-field' and '年龄' in c['text'] for c in parsed['chunks'])
    schema=tmp_path/'definition.json'
    schema.write_text(json.dumps({'entities':[
        {'name':'自然人','properties':{'姓名':{'type':'string'},'年龄':{'type':'integer'}}},
        {'name':'警情','properties':{'地点':{'type':'string'}}}],
        'relations':[{'source':'自然人','target':'警情','code':'reported_incident'}]},ensure_ascii=False))
    structured=extract(schema)
    assert sum(x['kind']=='json-class' for x in structured['chunks'])==2
    assert sum(x['kind']=='json-field' for x in structured['chunks'])==3
    assert sum(x['kind']=='json-relation' for x in structured['chunks'])==1


def test_single_json_case_description_abstracts_types(tmp_path):
    p=tmp_path/'case.json'
    p.write_text(json.dumps({'案件描述':'李某报警，随后驾驶电动车离开。'},ensure_ascii=False))
    parsed=extract(p)
    chunks=[x for x in parsed['chunks'] if x['kind']=='prose']
    assert chunks
    out=brain.joint_extract_segment({'kind':'prose','content':chunks[0]['text'],'section':'JSON/描述'})
    assert '自然人' in {x['name'] for x in out['entities']}
    assert '李某' not in {x['name'] for x in out['entities']}


def test_native_relation_semantics_not_silently_lost_cross_branch(tmp_path):
    data=sample_native()
    data['relations'][0]['symmetric']=True
    data['relations'][0]['dependencies']=[{'kind':'entity','id':E1,'required':True}]
    p=tmp_path/'export.json';p.write_text(json.dumps(data))
    parsed=extract(p)
    piece=next({'document_id':'doc','project_id':'p','locator':c['locator'],
                 'content':c['text'],'kind':c['kind']} for c in parsed['chunks'] if c['kind']=='native-relation')
    clue=brain.joint_extract_segment(piece)
    current={'entities':[{'id':E1,'name':'自然人','kind':'class'},
                         {'id':E2,'name':'报警人','kind':'class'}], 'attributes':[], 'relations':[]}
    items,gaps=incremental.align_clues('RELATION',clue,current,piece,baseline=None)
    assert not items
    assert gaps and '不能无损承载' in gaps[0]['reason']


def test_json_instance_number_regex_does_not_reject_record_type():
    from app.ontology_abstraction import instance_reason
    assert instance_reason('警情记录')==''
    assert instance_reason('案件编号 202601234')
