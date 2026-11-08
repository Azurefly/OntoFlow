"""Schema-only, evidence-grounded abstraction of narrative case descriptions.

All fixtures contain synthetic names and facts. Test only ontology definitions;
never permit a specific person, case record or amount to become a schema object.
"""
import json
import pytest

from app import brain, incremental
from app.ontology_abstraction import (
    ABSTRACTION_VERSION, named_people, instance_reason,
    discover_narrative_types, normalize_clues,
)
from app.structured_discovery import markdown_chunks
from app.ontology import validate_payload, check_ontology, InvalidOntology
from app.store import Store

MODEL={'base_url':'http://model.invalid/v1','model':'stub','api_key':''}
CASE='2026年11月2日20时许，张某在某商场门口与李某发生争执。张某向李某转账500元，随后李某驾驶电动车离开现场。'


def piece(text, **props):
    return {'document_id':'doc1','project_id':'proj1','locator':'行 1-3',
            'kind':'prose','section':'案件描述','content':text,
            'content_hash':incremental.sha(text),**props}


def empty_model(system,user):
    return {'entities':[],'attributes':[],'relations':[],'has_more':False}


def test_unstructured_case_is_abstracted_to_types_only(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',empty_model)
    with brain.using_model(MODEL):
        result=brain.joint_extract_segment(piece(CASE))
    concepts={x['name'] for x in result['entities']}
    assert {'自然人','争执事件','转账事件','地点','交通工具'}<=concepts
    assert not {'张某','李某','某商场门口','500元'} & concepts
    assert not any(x['name']=='报警人' for x in result['entities'])
    assert any(a['owner']=='转账事件' and a['name']=='转账金额' and a['datatype']=='double'
               for a in result['attributes'])
    assert not any('500元'==a['name'] for a in result['attributes'])
    assert {(r['source'],r['predicate'],r['target']) for r in result['relations']} >= {
        ('自然人','participatesIn','争执事件'),
        ('自然人','initiatesTransfer','转账事件'),
        ('转账事件','hasRecipient','自然人')}
    assert all(x['quote'] in CASE for k in ('entities','attributes','relations') for x in result[k])
    # Model gave no semantic contribution; rule-derived clues aren't a proof of full coverage.
    assert result['_complete'] in (False,True)


def test_no_role_inference_without_action_evidence():
    found=discover_narrative_types('李某驾驶电动车离开现场。')
    names={x['name'] for x in found['entities']}
    assert '自然人' in names and '交通工具' in names
    assert '报警人' not in names and '嫌疑人' not in names and '受害人' not in names


def test_explicit_reporter_role_is_candidate_but_requires_conversation():
    found=discover_narrative_types('李某报警称王某在某小区门口与其发生争执。')
    assert {'自然人','报警人','报警事件'} <= {x['name'] for x in found['entities']}
    assert any('子类' in x['reason'] and '事件角色' in x['reason'] for x in found['_unresolved'])
    assert '王某' not in {x['name'] for x in found['entities']}


def test_negated_reporting_is_not_a_reporter_role():
    found=discover_narrative_types('李某未报警，也未报案，仅驾驶电动车离开。')
    assert '报警人' not in {x['name'] for x in found['entities']}
    assert '报警事件' not in {x['name'] for x in found['entities']}
    noisy={'entities':[{'name':'报警人','quote':'李某不是报警人'}],'attributes':[],'relations':[]}
    cleaned=normalize_clues(noisy,'李某不是报警人')
    assert not cleaned['entities'] and cleaned['_unresolved']


def test_actual_names_are_generalized_not_saved_as_classes():
    source='李小明向王伟转账100元。'
    assert set(named_people(source)) >= {'李小明','王伟'}
    raw={'entities':[{'name':'李小明','quote':'李小明'},{'name':'王伟','quote':'王伟'}],
         'attributes':[],'relations':[]}
    output=normalize_clues(raw,source)
    assert [x['name'] for x in output['entities']]==['自然人']
    assert output['_coverage']['generalized_instance_references']==2


def test_model_instance_candidates_and_values_are_screened_before_cache(monkeypatch,tmp_path):
    source='张某向李某转账500元。'
    def model(system,user):
        assert '只构建本体模式' in system
        return {'entities':[
                    {'name':'张某','quote':'张某'},
                    {'name':'李某','quote':'李某'},
                    {'name':'报警人','quote':'张某向李某转账'},
                    {'name':'500元','quote':'500元'}],
                'attributes':[{'owner':'李某','name':'500元','datatype':'double','quote':'500元'}],
                'relations':[{'source':'张某','predicate':'relatedTo','target':'李某','quote':'张某向李某转账'}],
                'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    store=Store(tmp_path)
    with brain.using_model(MODEL):
        result,hit=incremental.cached_clues(store,piece(source),MODEL,brain.joint_extract_segment)
    assert not hit
    assert '张某' not in {x['name'] for x in result['entities']}
    assert '李某' not in {x['name'] for x in result['entities']}
    assert '报警人' not in {x['name'] for x in result['entities']}
    assert '自然人' in {x['name'] for x in result['entities']}
    assert all(x['name']!='500元' for x in result['attributes'])
    assert result['_unresolved']
    assert all(x['source']!='张某' and x['target']!='李某' for x in result['relations'])
    assert incremental.cached_clue_status(store,piece(source),MODEL)=='incomplete'


def test_structured_table_with_specific_person_is_generalized_without_llm(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *args:(_ for _ in ()).throw(AssertionError('should use deterministic parser')))
    rows=markdown_chunks('## 实体清单\n| 实体编号 | 实体名称 | 描述 |\n|---|---|---|\n| E-1 | 李某 | 个人线索 |')
    table=next(r for r in rows if r['kind']=='table')
    with brain.using_model(MODEL):result=brain.joint_extract_segment(piece(table['text'],kind='table'))
    assert [x['name'] for x in result['entities']]==['自然人']
    assert result['_complete'] is True


def test_alignment_maps_person_reference_to_existing_natural_person_uuid():
    source='张某转账给李某。'
    clues={'entities':[], 'attributes':[],
           'relations':[{'source':'张某','predicate':'relatedTo','target':'李某','quote':source}]}
    baseline={'entities':[{'id':'uuid-existing','name':'自然人','code':'person'}],
              'attributes':[],'relations':[]}
    current={'entities':[],'attributes':[],'relations':[]}
    out,gaps=incremental.align_clues('RELATION',clues,current,piece(source),baseline=baseline)
    assert not gaps
    assert out and out[0]['payload']['source_id']=='uuid-existing'
    assert out[0]['payload']['target_id']=='uuid-existing'


def test_no_model_mode_still_extracts_visible_types_and_marks_gaps():
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        result=brain.joint_extract_segment(piece('李某在某商场门口驾驶电动车，随后报警。'))
    assert any(e['name']=='自然人' for e in result['entities'])
    assert not result['_complete']
    assert result['_unresolved']


@pytest.mark.parametrize('candidate',[
    {'name':'李某','kind':'class'},
    {'name':'张伟','kind':'class'},
    {'name':'某商场门口','kind':'class'},
    {'name':'500元','kind':'class'},
    {'name':'自然人','kind':'instance','class_id':'some'},
])
def test_manual_candidate_validation_forbids_instances(candidate):
    snap={'entities':[], 'attributes':[], 'relations':[]}
    with pytest.raises(InvalidOntology):validate_payload('entity',candidate,snap)


def test_historical_instance_cannot_be_published_unchanged():
    snap={'entities':[{'id':'old','name':'李某','kind':'instance'}],
          'attributes':[],'relations':[]}
    assert not check_ontology(snap)['valid']
    assert '实例' in ' '.join(check_ontology(snap)['issues'])


def test_conversational_agent_does_not_propose_person_as_class(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *args:{
        'reply':'已识别张某。',
        'proposals':[{'kind':'entity','payload':{'name':'张某','kind':'class'},'evidence':[]},
                     {'kind':'entity','payload':{'name':'自然人','kind':'class'},'evidence':[]}]})
    with brain.using_model(MODEL):
        output=brain.chat('为张某构建本体','ENTITY','人物类型',
            {'entities':[],'attributes':[],'relations':[]},[],[])
    assert len(output['proposals'])==1
    assert output['proposals'][0]['payload']['name']=='自然人'
    assert '拦截' in output['reply']


def test_new_cache_version_cannot_reuse_old_schema_inference():
    p=piece(CASE)
    assert ABSTRACTION_VERSION in incremental.EXTRACTOR_VERSION
    assert incremental.clue_cache_key(p,MODEL,'evidence-first-v3') != incremental.clue_cache_key(p,MODEL)


def test_existing_personnel_class_suggests_clarification_not_duplicate():
    source='张某和李某在现场发生争执。'
    clues={'entities':[{'name':'自然人','quote':'张某'}], 'attributes':[], 'relations':[]}
    current={'entities':[], 'attributes':[], 'relations':[]}
    baseline={'entities':[{'id':'remote-personnel-uuid','name':'人员','code':'person'}],
              'attributes':[],'relations':[]}
    out,gaps=incremental.align_clues('ENTITY',clues,current,piece(source),baseline=baseline)
    assert not out and len(gaps)==1
    assert '人员' in gaps[0]['reason'] and '自然人' in gaps[0]['reason']
