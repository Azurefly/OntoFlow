"""Regression for real cancelled inference: bound output and source-level retries."""
import json
import httpx
from app import brain,incremental
from app.semantic_contract import output_schema,validate_response
from app.store import Store
CONF={'base_url':'http://model.invalid/v1','model':'synthetic-contract','api_key':''}

def piece(text):
    return {'document_id':'d','project_id':'p','kind':'prose','section':'业务正文','locator':'段落1',
            'content':text,'content_hash':incremental.sha(text)}

def test_decoder_schema_is_bounded_without_domain_answers():
    s=output_schema()['properties']
    for key in ('entities','attributes','relations'):
        assert s[key]['maxItems']==6
        assert s[key]['items']['properties']['quote']['maxLength']==160
        assert s[key]['items']['properties']['description']['maxLength']==80
    assert s['questions']['maxItems']==3

def test_long_quote_not_silently_truncated_or_complete():
    text='证据材料'+('审核字段。'*35)
    raw={'entities':[{'name':'证据材料','code':'evidence_material','quote':text}], 'attributes':[], 'relations':[]}
    out,err=validate_response(raw,text)
    assert not out['entities'] and any('160' in e for e in err)
    assert raw['entities'][0]['quote']==text

def test_semantic_length_has_one_http_request_not_three(monkeypatch):
    calls=[]
    def handler(r):
        calls.append(json.loads(r.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"entities":['},'finish_reason':'length'}]})
    monkeypatch.setattr(brain,'_pooled_http_client',lambda:httpx.Client(transport=httpx.MockTransport(handler)))
    with brain.using_model(CONF),brain.using_output_schema(output_schema()):
        try:brain.query_llm('schema contract','source')
        except brain.ModelOutputTruncated:pass
        else:assert False,'Truncated model output cannot be accepted'
    assert len(calls)==1

def test_split_after_truncation_keeps_every_source_character(monkeypatch):
    text=('业务事项应当有完整的办理材料与审核依据。\n'*30)
    calls=[];states=[]
    def model(*args):calls.append(1);raise brain.ModelOutputTruncated('budget')
    monkeypatch.setattr(brain,'query_llm',model)
    monkeypatch.setenv('JOINT_EXTRACT_CALL_BUDGET','1')
    with brain.using_model({**CONF,'interpretation':{'reading_mode':'semantic_review'}}):
        out=brain.joint_extract_segment(piece(text),on_checkpoint=states.append)
    assert len(calls)==1 and out['_complete'] is False
    assert len(out['_pending'])==2
    assert ''.join(u['text'] for u in out['_pending'])==text
    assert out['_coverage']['truncated_responses']==1
    assert states[-1]['_pending']==out['_pending']

def test_invalid_json_resume_budget_is_finite(monkeypatch,tmp_path):
    calls=[]
    def model(*a):calls.append(1);raise brain.ModelFormatError('bad json')
    monkeypatch.setattr(brain,'query_llm',model)
    monkeypatch.setenv('JOINT_MAX_RETRY_ROUNDS','2')
    store=Store(tmp_path);p=piece('材料提交之后由业务人员审核。')
    with brain.using_model(CONF):
        for _ in range(5):result,hit=incremental.cached_clues(store,p,CONF,brain.joint_extract_segment)
    assert len(calls)==2
    assert result['_state']=='terminal_invalid' and not result['_complete']
    assert not hit

def test_incremental_repair_keeps_good_items_and_provides_type_inventory(monkeypatch):
    calls=[]
    def model(system,user):
        q=json.loads(user);calls.append(q)
        if len(calls)==1:
            return {'entities':[{'name':'材料','code':'material','quote':'材料'},
                    {'name':'审核','code':'bad code','quote':'审核'}],
                    'attributes':[],'relations':[],'questions':['审核角色需要单独建类吗？'],'has_more':False}
        return {'entities':[{'name':'审核','code':'review','quote':'审核'}],
                'attributes':[],'relations':[],'questions':['审核角色需要单独建类吗？'],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    with brain.using_model(CONF):
        first=brain.joint_extract_segment(piece('材料经审核后归档。'))
        second=brain.joint_extract_segment(piece('材料经审核后归档。'),checkpoint=first)
    assert len(calls)==2 and {'name':'材料','code':'material'} in calls[1]['accepted_types']
    assert {x['name'] for x in second['entities']}=={'材料','审核'}
    assert second['_complete']
    assert len([x for x in second['_unresolved'] if x.get('category')=='semantic_clarification'])==1
