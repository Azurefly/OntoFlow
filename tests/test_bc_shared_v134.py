"""BC-specific structure, shared Attribute.entityIds, and proposal dedup tests."""
import json
import uuid
import os
import pytest
from pathlib import Path

from app.extraction import extract
from app.structured_discovery import deterministic_discover
from app.schema_catalog import build_attribute_catalog, verified_source_attribute_code
from app.openmind import make_plan
from app.incremental import align_clues
from app.proposal_dedup import proposal_identity, compatibility_issue, merge_evidence

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'


def _attribute(aid, owner, name, code, datatype='string'):
    return {'id':aid,'owner_id':owner,'name':name,'datatype':datatype,
            'multivalued':False,'required':False,'description':'测试共享字段',
            'evidence':json.dumps([{'document_id':'doc1','locator':'行 300-400 / 6. 实体属性（按实体）',
               'quote':f'| 1 | {code} | {name} | 字符串 | 业务属性 | 否 | SRC.VALUE | 测试 |',
               'verified':True}],ensure_ascii=False)}


def test_shared_attribute_becomes_one_native_object_with_multi_entity_ids():
    snap={'project':{'revision':2},'entities':[
            {'id':'a','name':'用户','kind':'class','description':'用户'},
            {'id':'b','name':'信源','kind':'class','description':'信源'}],
          'attributes':[_attribute('one','a','创建时间','cjsj'),_attribute('two','b','创建时间','cjsj')],
          'relations':[]}
    plan=make_plan(snap,'project-id',DOMAIN,BRANCH)
    assert plan['ready'],plan['blockers']
    attrs=[s for s in plan['steps'] if s['kind']=='attribute']
    assert len(attrs)==1
    assert attrs[0]['data']['code']=='cjsj'
    assert attrs[0]['local_ids']==['one','two']
    assert len(attrs[0]['data']['entityIds'])==2
    assert len(set(attrs[0]['data']['entityIds']))==2
    assert plan==make_plan(snap,'project-id',DOMAIN,BRANCH)


def test_conflicting_group_type_blocks_plan():
    snap={'project':{'revision':1},'entities':[
        {'id':'a','name':'甲','kind':'class','description':''},
        {'id':'b','name':'乙','kind':'class','description':''}],
        'attributes':[_attribute('one','a','创建时间','cjsj','date'),
                      _attribute('two','b','创建时间','cjsj','string')],
        'relations':[]}
    plan=make_plan(snap,'p',DOMAIN,BRANCH)
    assert not plan['ready']
    assert any('多个名称或数据类型' in x for x in plan['blockers'])


def test_parent_and_child_direct_binding_of_one_property_is_blocked():
    snap={'project':{'revision':1},'entities':[
        {'id':'a','name':'甲','kind':'class','description':''},
        {'id':'b','name':'乙','kind':'class','description':''}],
        'attributes':[_attribute('one','a','创建时间','cjsj'),
                      _attribute('two','b','创建时间','cjsj')],
        'relations':[{'id':'r','source_id':'b','target_id':'a',
                      'predicate':'subClassOf','description':'乙继承甲'}]}
    plan=make_plan(snap,'p',DOMAIN,BRANCH)
    assert not plan['ready']
    assert any('继承重复属性' in x for x in plan['blockers'])


def test_manual_attributes_without_source_code_are_not_automatically_merged():
    snap={'project':{'revision':1},'entities':[
        {'id':'a','name':'甲','kind':'class','description':''},
        {'id':'b','name':'乙','kind':'class','description':''}],
        'attributes':[_attribute('one','a','创建时间','cjsj'),
                      _attribute('two','b','创建时间','cjsj')],
        'relations':[]}
    for a in snap['attributes']:a['evidence']='[]'
    plan=make_plan(snap,'p',DOMAIN,BRANCH)
    assert plan['counts']['attribute']==2
    assert plan['ready'],plan['blockers']


def test_existing_same_name_remote_attribute_blocks_new_duplicate():
    snap={'project':{'revision':1},'entities':[
        {'id':'a','name':'甲','kind':'class','description':''}],
        'attributes':[_attribute('one','a','创建时间','cjsj')],'relations':[]}
    catalog={'entities':[],'attributes':[{'id':str(uuid.uuid4()),'code':'cjsj',
        'name':'创建时间','dataType':'string','entityIds':[]}],
        'relations':[],'actions':[],'capabilities':[]}
    plan=make_plan(snap,'p',DOMAIN,BRANCH,catalog=catalog)
    assert not plan['ready']
    assert any('已有同名同类型共享属性' in x for x in plan['blockers'])


def test_source_code_with_dash_is_normalized_for_openmind():
    snap={'project':{'revision':1},'entities':[
        {'id':'a','name':'甲','kind':'class','description':''}],
        'attributes':[_attribute('one','a','测试字段','xybh-2')],
        'relations':[]}
    plan=make_plan(snap,'p',DOMAIN,BRANCH)
    assert plan['ready'],plan['blockers']
    assert next(s for s in plan['steps'] if s['kind']=='attribute')['data']['code']=='xybh_2'


def test_semantic_proposals_are_deduped_by_identity_not_description():
    a={'name':'信源','kind':'class','description':''}
    b={'name':'信源','kind':'class','description':'数据来源类型'}
    assert proposal_identity('entity',a)==proposal_identity('entity',b)
    assert compatibility_issue('entity',a,b)==''
    assert merge_evidence([{'document_id':'a','quote':'定义','locator':'L1'}],
                          [{'document_id':'a','quote':'定义','locator':'L1'},
                           {'document_id':'b','quote':'概述','locator':'L2'}])==[
                            {'document_id':'a','quote':'定义','locator':'L1'},
                            {'document_id':'b','quote':'概述','locator':'L2'}]


def test_source_catalog_crosschecks_real_bc_document_if_available():
    path=Path(os.getenv('ONTOFLOW_BC_FIXTURE','/mnt/data/信息分发本体设计 (BC 项目)-v0.1.md'))
    if not path.is_file():pytest.skip('Set ONTOFLOW_BC_FIXTURE to run the real-document test')
    source=extract(path)
    chunks=source['chunks']
    result=build_attribute_catalog([{'chunks':chunks}])
    assert result['counts']=={'shared_attributes':283,'direct_bindings':373,
                                'entities_with_properties':22,'issues':0}
    values={'entities':[],'attributes':[],'relations':[]}
    for chunk in chunks:
        if chunk['kind']=='heading':continue
        found=deterministic_discover({'content':chunk['text'],
                 'section':chunk['section'],'kind':chunk['kind']})
        for k in values:values[k].extend(found[k])
    assert len(values['entities'])==22
    assert len({x['name'] for x in values['entities']})==22
    assert len(values['relations'])==48
    assert len(values['attributes'])==373
    assert len({x['owner'] for x in values['attributes']})==22
    assert len({x['code'] for x in values['attributes']})==283
    assert set(result['definitions'])=={x['code'] for x in values['attributes']}


def test_alignment_against_shared_catalog_rejects_wrong_owner():
    doc=[{'chunks':[{'kind':'table','section':'X','text':'| 属性编号 | 属性名称 | 数据类型 |'}]}]
    cat={'definitions':{'xybh':{'name':'信源编号','datatype':'string',
                                'expected_owners':['信源']}},'issues':[]}
    piece={'content':'| 1 | xybh | 信源编号 | 字符串 |',
           'locator':'L1','document_id':'d'}
    clues={'attributes':[{'owner':'用户','code':'xybh','name':'信源编号',
                          'datatype':'string','quote':piece['content']}]}
    current={'entities':[{'id':'a','name':'信源','kind':'class'},
                         {'id':'b','name':'用户','kind':'class'}],
             'attributes':[],'relations':[]}
    got,gaps=align_clues('PROPERTY',clues,current,piece,catalog_reference=cat)
    assert not got and gaps
    assert any('共享目录' in x['reason'] for x in gaps)
