"""Contract failures observed in actual inference, not fabricated domain answers."""
import json
import pytest
from app import brain,incremental
from app.semantic_contract import validate_response, locate_quote, output_schema
from app.store import Store

CONF={'base_url':'http://model.invalid/v1','model':'test','api_key':''}


def piece(text):
    return {'document_id':'d','project_id':'p','kind':'prose','section':'业务内容',
            'content':text,'content_hash':incremental.sha(text),'locator':'段落1'}


def response(entities=None,attributes=None,relations=None):
    return {'entities':entities or [],'attributes':attributes or [],'relations':relations or [],'has_more':False}


def test_chinese_code_not_cached_complete(monkeypatch,tmp_path):
    text='业务人员核对业务材料。'
    monkeypatch.setattr(brain,'query_llm',lambda *a:response([{'name':'业务人员','code':'业务人员','quote':'业务人员'}]))
    db=Store(tmp_path)
    with brain.using_model(CONF):out,hit=incremental.cached_clues(db,piece(text),CONF,brain.joint_extract_segment)
    assert not hit and not out['_complete'] and out['_state']=='retryable'
    assert not out['entities']
    assert 'code' in str(out['_retry_units'][0]['validation_feedback'])
    assert incremental.cached_clue_status(db,piece(text),CONF)=='incomplete'


def test_feedback_survives_checkpoint_and_can_correct_not_invent(monkeypatch,tmp_path):
    text='业务人员核对业务材料。';calls=[]
    def model(system,user):
        data=json.loads(user);calls.append(data)
        return response([{'name':'业务人员','code':'业务人员' if len(calls)==1 else 'business_staff',
                          'quote':'业务人员'}])
    monkeypatch.setattr(brain,'query_llm',model);db=Store(tmp_path)
    with brain.using_model(CONF):
        first,_=incremental.cached_clues(db,piece(text),CONF,brain.joint_extract_segment)
        second,_=incremental.cached_clues(db,piece(text),CONF,brain.joint_extract_segment)
    assert not first['_complete'] and second['_complete']
    assert calls[1]['piece']==calls[0]['piece']==text
    assert 'code' in str(calls[1]['validation_feedback'])
    assert second['entities'][0]['code']=='business_staff'
    assert second['entities'][0]['quote'] in text
    assert second['_coverage']['contract_rejections']>=1


def test_no_quote_not_reconstructed_from_name(monkeypatch):
    raw=response([{'name':'业务材料','code':'business_material'}])
    cleaned,errors=validate_response(raw,'业务材料提交审核。')
    assert not cleaned['entities'] and any('quote' in e for e in errors)
    assert raw['entities'][0].get('quote') is None


def test_invalid_datatype_not_coerced():
    raw=response(attributes=[{'owner':'业务材料','name':'附加数据','code':'extra_data','datatype':'object','quote':'附加数据'}])
    cleaned,errors=validate_response(raw,'附加数据可能包含复杂字段。')
    assert not cleaned['attributes'] and any('datatype' in e for e in errors)


def test_natural_language_predicate_is_not_valid_code():
    raw=response(relations=[{'source':'业务人员','target':'业务材料','name':'核对','code':'checks',
                             'predicate':'业务人员核对业务材料','quote':'业务人员核对业务材料'}])
    cleaned,errors=validate_response(raw,'业务人员核对业务材料。')
    assert not cleaned['relations'] and any('predicate' in e for e in errors)


def test_layout_alignment_preserves_verbatim_source():
    source='人员提交材\n料，办理业务。'
    exact=locate_quote(source,'提交材料')
    assert exact=='提交材\n料' and exact in source
    cleaned,errors=validate_response(response([{'name':'材料','quote':'提交材料','code':'material'}]),source)
    assert not errors and cleaned['entities'][0]['quote']==exact
    assert cleaned['entities'][0]['_quote_alignment']=='layout_whitespace_only'


def test_english_words_and_paraphrases_not_fuzzy_matched():
    assert locate_quote('They are now here.','nowhere') is None
    assert locate_quote('人员核对材料。','人员审批材料') is None
    assert locate_quote('资料为甲。资料为乙。','资料为丙') is None


def test_ambiguous_layout_equivalence_is_not_guessed():
    assert locate_quote('提交材\n料。提交材 料。','提交材料') is None


def test_schema_does_not_contain_placeholder_ontology_answers():
    schema=output_schema()
    assert set(schema['properties'])=={'entities','attributes','relations','questions','has_more'}
    assert '原文子串' not in json.dumps(schema,ensure_ascii=False)
    assert schema['properties']['attributes']['items']['properties']['datatype']['enum']==['string','integer','double','boolean','date']


def test_cache_isolated_by_structured_output_configuration():
    p=piece('业务人员核对业务材料。')
    assert incremental.clue_cache_key(p,CONF)!=incremental.clue_cache_key(p,{**CONF,'structured_output':'json_schema'})


def test_retry_budget_does_not_call_forever(monkeypatch,tmp_path):
    monkeypatch.setenv('JOINT_MAX_RETRY_ROUNDS','2')
    called=[]
    def bad(*a):called.append(1);return response([{'name':'材料','code':'材料','quote':'材料'}])
    monkeypatch.setattr(brain,'query_llm',bad);db=Store(tmp_path);p=piece('材料提交。')
    with brain.using_model(CONF):
        for _ in range(5):last,_=incremental.cached_clues(db,p,CONF,brain.joint_extract_segment)
    assert len(called)==2 and last['_state']=='terminal_invalid' and not last['_complete']


def test_opt_in_provider_schema_is_sent_without_secret_leak(monkeypatch):
    import httpx
    requests=[]
    def dispatch(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(response())},'finish_reason':'stop'}]})
    client=httpx.Client(transport=httpx.MockTransport(dispatch))
    monkeypatch.setattr(brain,'_pooled_http_client',lambda:client)
    with brain.using_model({**CONF,'structured_output':'json_schema'}),brain.using_output_schema(output_schema()):
        brain.query_llm('schema test','source')
    assert requests[0]['response_format']['json_schema']['schema']==output_schema()
    with brain.using_model(CONF):brain.query_llm('default test','source')
    assert 'response_format' not in requests[1]
