"""Narrative-to-schema regression: attribution, negation, ambiguity and caching.

Only synthetic data is used. These tests do not assert that narration proves a
crime or verifies the underlying events; they test suggested *type* definitions.
"""
from app import brain,incremental
from app.ontology_abstraction import discover_narrative_types, normalize_clues
from app.store import Store

MODEL={'base_url':'http://model.invalid/v1','model':'stub','api_key':''}

def names(result):
    return {item['name'] for item in result['entities']}

def rels(result):
    return {(x['source'],x['predicate'],x['target']) for x in result['relations']}

def test_clear_cross_sentence_pronoun_reporter_is_type_not_person():
    text='李某来到派出所。随后其报警。'
    result=discover_narrative_types(text)
    assert {'自然人','报警人','报警事件'}<=names(result)
    assert '李某' not in names(result)
    assert ('自然人','reportsIncident','报警事件') in rels(result)
    assert any('指代' in x['reason'] and x['blocks_coverage'] is False for x in result['_unresolved'])
    assert all(x['quote'] in text for x in result['entities']+result['relations'])


def test_two_people_ambiguous_pronoun_stops_role_attribution():
    text='张某与李某发生争执。随后其报警。'
    result=discover_narrative_types(text)
    assert '报警人' not in names(result)
    assert ('自然人','reportsIncident','报警事件') not in rels(result)
    assert any(x.get('category')=='coreference_ambiguity' for x in result['_unresolved'])


def test_explicit_person_after_two_people_is_grounded():
    text='张某与李某发生争执。随后李某报警。'
    result=discover_narrative_types(text)
    assert '报警人' in names(result)
    assert ('自然人','reportsIncident','报警事件') in rels(result)


def test_negated_reporting_does_not_become_reporter_or_event():
    for text in ('李某未报警。','李某没有向派出所报警。',
                 '李某否认报警。','李某并未报案。'):
        result=discover_narrative_types(text)
        assert '报警人' not in names(result),text
        assert '报警事件' not in names(result),text
        assert not any(x['predicate']=='reportsIncident' for x in result['relations'])


def test_explicit_report_and_driver_have_separate_role_candidates():
    text='李某向派出所报案，随后李某驾驶电动车离开。'
    result=discover_narrative_types(text)
    assert {'自然人','报案人','驾驶人','交通工具','报警事件'}<=names(result)
    assert ('自然人','drives','交通工具') in rels(result)
    assert ('自然人','reportsIncident','报警事件') in rels(result)
    assert not any(x['predicate'] in ('owns','isOwnerOf') for x in result['relations'])
    assert sum(x.get('category')=='modeling_choice' for x in result['_unresolved'])>=2


def test_denied_transfer_has_no_transfer_schema_definition():
    text='张某否认向李某转账500元。'
    result=discover_narrative_types(text)
    assert '转账事件' not in names(result)
    assert not result['attributes']
    assert not any(x['predicate'] in ('initiatesTransfer','hasRecipient') for x in result['relations'])


def test_attributed_transfer_does_not_use_reporter_as_source():
    text='王某称张某向李某转账500元。'
    result=discover_narrative_types(text)
    assert ('自然人','initiatesTransfer','转账事件') in rels(result)
    assert '王某' not in names(result)


def test_model_returning_no_new_narrative_clues_does_not_invalidate_rule_completion(monkeypatch,tmp_path):
    monkeypatch.setattr(brain,'query_llm',lambda *a:{'entities':[],'attributes':[],
                                                     'relations':[],'has_more':False})
    text='李某报警。'
    piece={'content':text,'content_hash':incremental.sha(text),'section':'案件叙述',
           'kind':'prose','document_id':'doc','locator':'行 1'}
    store=Store(tmp_path)
    with brain.using_model(MODEL):
        out,hit=incremental.cached_clues(store,piece,MODEL,brain.joint_extract_segment)
        repeat,reuse=incremental.cached_clues(store,piece,MODEL,brain.joint_extract_segment)
    assert not hit and reuse and out==repeat
    assert out['_complete'] is True
    assert '报警人' in names(out)
    assert out['_unresolved'] and all(g['blocks_coverage'] is False for g in out['_unresolved'])


def test_unclear_pronoun_stays_incomplete_after_model_abstains(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *a:{'entities':[],'attributes':[],
                                                     'relations':[],'has_more':False})
    text='张某和李某发生争执。随后其报警。'
    with brain.using_model(MODEL):
        out=brain.joint_extract_segment({'content':text,'kind':'prose','section':'案件'})
    assert not out['_complete']
    assert any('指代不明确' in v['reason'] for v in out['_unresolved'])
    assert '报警人' not in names(out)


def test_provided_model_role_without_evidence_is_rejected():
    text='李某未报警。'
    result=normalize_clues({'entities':[{'name':'报警人','quote':'李某未报警'}],
                            'attributes':[],'relations':[]},text)
    assert '报警人' not in names(result)
    assert result['_unresolved']


def test_role_words_are_not_positive_incident_actions():
    text='本体设计建议包含报警人、报案人和驾驶人三类人员角色。'
    result=discover_narrative_types(text)
    assert '报警事件' not in names(result)
    assert '报警人' not in names(result)  # no actor/evidence of an event


def test_explicit_named_reporter_label_is_role_not_new_instance_event():
    text='报警人为李某。'
    result=discover_narrative_types(text)
    assert '报警人' in names(result)
    assert '李某' not in names(result)
    assert '报警事件' not in names(result)
    assert any(x.get('category')=='modeling_choice' for x in result['_unresolved'])


def test_unrelated_amount_does_not_become_transfer_attribute():
    result=discover_narrative_types('张某向李某转账。随后民警在现场发现500元现金。')
    assert '转账事件' in names(result)
    assert not result['attributes']


def test_reported_speech_is_flagged_as_unverified_fact():
    result=discover_narrative_types('王某称张某向李某转账500元。')
    assert any(x.get('category')=='reported_fact' for x in result['_unresolved'])
    assert '王某' not in names(result)


def test_model_cannot_infer_legal_status_from_injury_or_suspicion():
    raw={'entities':[{'name':'受害人','quote':'李某受伤'},
                     {'name':'嫌疑人','quote':'张某涉嫌盗窃'}],
         'attributes':[],'relations':[]}
    out=normalize_clues(raw,'李某受伤；张某涉嫌盗窃')
    assert not {'受害人','嫌疑人'} & names(out)
    assert len(out['_unresolved'])==2


def test_model_role_in_denial_speech_cannot_pass_grounding():
    text='李某否认自己报警。'
    raw={'entities':[{'name':'报警人','quote':'李某否认自己报警'}],
         'attributes':[],'relations':[]}
    out=normalize_clues(raw,text)
    assert '报警人' not in names(out)
    assert out['_unresolved']


def test_model_cannot_create_unsupported_business_roles_without_terms():
    text='张某向李某转账500元。'
    raw={'entities':[{'name':'报案人','quote':'张某向李某转账500元'},
                     {'name':'审核人','quote':'张某向李某转账500元'}],
         'attributes':[],'relations':[]}
    out=normalize_clues(raw,text)
    assert not {'报案人','审核人'} & names(out)


def test_chat_assistant_receives_persistent_ambiguity_question(monkeypatch):
    import json
    captured={}
    def model(system,user):
        captured['request']=json.loads(user)
        return {'reply':'请确认“其”指向哪位自然人，并决定是否建报警人子类。','proposals':[]}
    monkeypatch.setattr(brain,'query_llm',model)
    questions=[{'kind':'coreference_ambiguity','reason':'其指代不明确',
                'payload':{'source_excerpt':'张某与李某争执。随后其报警。'}}]
    with brain.using_model(MODEL):
        result=brain.chat('这里的其指谁？','ENTITY','案件类型模型',
            {'entities':[],'attributes':[],'relations':[]},[],[],clarifications=questions)
    assert captured['request']['unresolved_clarifications']==questions
    assert result['proposals']==[]
    assert '请确认' in result['reply']
