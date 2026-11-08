"""Fresh counterexamples for subjective input; no BC answers encoded here.

Synthetic DOCX/Markdown and stub LLM outputs distinguish file parsing, source
claims, model inference, explicit human intent, and stale-candidate authority.
"""
from __future__ import annotations
import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image

from app import brain,incremental,modeling
from app.main import create_app
from app.extraction import extract
from app.structured_discovery import deterministic_discover,markdown_chunks
from app.store import Store
from app.source_understanding import SOURCE_VERSION,material_profile

MODEL={'base_url':'https://model.invalid/v1','model':'mock','api_key':''}


def piece(text,**kw):
    return {'content':text,'content_hash':incremental.sha(text),'kind':'prose',
            'section':'','locator':'测试单元','document_id':'d','project_id':'p',**kw}


def intent(goal='',**kw):
    data={**modeling.DEFAULT,'project_id':'p','goal':goal,'confirmed_scope':'类型定义',
          'baseline_fingerprint':'base-1',**kw}
    return {**data,'fingerprint':modeling.digest(data),'decision_version':1}


def config(goal='',**kw):return {**MODEL,'interpretation':intent(goal,**kw)}


def empty_model(*args):return {'entities':[],'attributes':[],'relations':[],'has_more':False}


def create_table(document,rows):
    table=document.add_table(rows=0,cols=len(rows[0]))
    for values in rows:
        cells=table.add_row().cells
        for cell,value in zip(cells,values):cell.text=value
    return table


def test_word_body_order_and_per_table_owner(tmp_path):
    d=Document();d.add_heading('设备的属性定义',1)
    create_table(d,[['属性名称','数据类型'],['设备编号','string']])
    d.add_heading('自然人的属性定义',1)
    create_table(d,[['属性名称','数据类型'],['姓名','string']])
    path=tmp_path/'自由文件名.docx';d.save(path)
    data=extract(path)
    assert [x['kind'] for x in data['chunks']]==['heading','table','heading','table']
    attrs=[a for c in data['chunks'] if c['kind']=='table'
           for a in deterministic_discover({**c,'content':c['text']})['attributes']]
    assert [(a['owner'],a['name']) for a in attrs]==[('设备','设备编号'),('自然人','姓名')]
    assert data['warnings']==[]
    assert [c['source_order'] for c in data['chunks']]==sorted(c['source_order'] for c in data['chunks'])


def test_same_content_markdown_and_word_same_schema(tmp_path):
    d=Document();d.add_heading('资源台账',1)
    create_table(d,[['实体名称','实体编号','描述'],['馆藏品','collection_item','可管理的馆藏类型']])
    d.add_heading('馆藏品的属性定义',2)
    create_table(d,[['属性名称','数据类型'],['保管状态','string']])
    path=tmp_path/'a.docx';d.save(path)
    word=extract(path)['chunks']
    md=markdown_chunks('# 资源台账\n| 实体名称 | 实体编号 | 描述 |\n|---|---|---|\n| 馆藏品 | collection_item | 可管理的馆藏类型 |\n\n## 馆藏品的属性定义\n| 属性名称 | 数据类型 |\n|---|---|\n| 保管状态 | string |')
    def items(chunks):
        out=[]
        for c in chunks:
            found=deterministic_discover({**c,'content':c['text']})
            for k in ('entities','attributes','relations'):
                out.extend((k,x.get('owner'),x.get('name'),x.get('datatype')) for x in found[k])
        return out
    assert items(word)==items(md)


def test_heading_inherited_from_custom_style_is_preserved(tmp_path):
    from docx.enum.style import WD_STYLE_TYPE
    d=Document();style=d.styles.add_style('用户自定章节',WD_STYLE_TYPE.PARAGRAPH)
    style.base_style=d.styles['Heading 2']
    d.add_paragraph('设备的属性定义',style=style)
    create_table(d,[['字段名称','数据类型'],['编号','string']])
    p=tmp_path/'custom.docx';d.save(p)
    out=extract(p)
    assert out['chunks'][0]['kind']=='heading'
    assert out['chunks'][1]['section']=='设备的属性定义'


def test_no_heading_never_invents_table_as_entity(tmp_path):
    d=Document();d.add_paragraph('关于这个字段，有待业务人员确定所属对象。')
    create_table(d,[['字段名称','数据类型'],['编号','string']])
    p=tmp_path/'noheading.docx';d.save(p)
    table=next(c for c in extract(p)['chunks'] if c['kind']=='table')
    found=deterministic_discover({**table,'content':table['text']})
    assert found['attributes']==[]
    assert any('所属' in g['reason'] for g in found['_unresolved'])
    assert found['_semantic_text'] and '字段名称' in found['_semantic_text']
    assert '业务人员' in table['context_before']


def test_merged_table_is_retained_but_not_guessed(tmp_path,monkeypatch):
    d=Document();d.add_heading('设备的属性定义',1)
    table=create_table(d,[['字段名','类型'],['范围','字段待细分']]);table.cell(1,0).merge(table.cell(1,1))
    p=tmp_path/'merged.docx';d.save(p)
    data=extract(p)
    unit=next(c for c in data['chunks'] if c['kind']=='ambiguous_table')
    assert '范围' in unit['text'] and data['warnings']
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        out=brain.joint_extract_segment(piece(unit['text'],**{k:v for k,v in unit.items() if k!='text'}))
    assert not out['attributes']
    assert out['_unresolved']


def test_word_images_are_coverage_warning_not_successful_ocr(tmp_path):
    d=Document();d.add_paragraph('正文可读，但截图内还有定义。')
    png=io.BytesIO();Image.new('RGB',(20,20),'white').save(png,format='PNG');png.seek(0)
    d.add_picture(png)
    p=tmp_path/'mixed.docx';d.save(p)
    out=extract(p)
    assert any('图片' in w and '未识别' in w for w in out['warnings'])
    document={'id':'d','filename':p.name,**out}
    profile=material_profile([document]);assert not profile['complete']
    with brain.using_model({'base_url':'','model':'','api_key':''}):summary=brain.analyze([document])
    assert summary['coverage']['complete'] is False
    assert summary['questions']


def test_word_header_footer_and_textbox_are_separate_regions(tmp_path):
    d=Document();d.add_paragraph('主体内容')
    d.sections[0].header.paragraphs[0].text='页眉保留内容'
    d.sections[0].footer.paragraphs[0].text='页脚保留内容'
    p=d.add_paragraph('有文本框的段落')
    box=OxmlElement('w:txbxContent');bp=OxmlElement('w:p');br=OxmlElement('w:r');bt=OxmlElement('w:t')
    bt.text='浮动文本框的业务定义';br.append(bt);bp.append(br);box.append(bp);p._p.append(box)
    path=tmp_path/'regions.docx';d.save(path)
    out=extract(path)
    assert all(text in out['content'] for text in ('主体内容','页眉保留内容','页脚保留内容','浮动文本框的业务定义'))
    boxchunks=[c for c in out['chunks'] if c['kind']=='textbox']
    assert len(boxchunks)==1 and '文本框' in boxchunks[0]['locator']
    assert any('浮动位置' in w for w in out['warnings'])


def test_source_cache_reuses_evidence_not_other_goal_answers(tmp_path,monkeypatch):
    store=Store(tmp_path);calls=[]
    def model(system,user):
        req=json.loads(user);goal=req['modeling_intent']['goal'];calls.append(goal)
        name='材料类型' if goal=='资料管理' else '审查活动'
        return {'entities':[{'name':name,'quote':'材料交给审查人员处理'}],
                'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    p=piece('材料交给审查人员处理，流程定义需要结合本次目标确定。')
    outputs=[]
    for goal in ('资料管理','审查流程','审查流程'):
        conf=config(goal)
        with brain.using_model(conf):outputs.append(incremental.cached_clues(store,p,conf,brain.joint_extract_segment))
    assert calls==['资料管理','审查流程']
    assert [hit for _,hit in outputs]==[False,False,True]
    assert outputs[0][0]['entities'][0]['name']=='材料类型'
    assert outputs[1][0]['entities'][0]['name']=='审查活动'
    neutral=store.many('SELECT * FROM extraction_cache WHERE extractor_version=?',(SOURCE_VERSION,))
    assert len(neutral)==1
    assert len(store.many('SELECT * FROM extraction_cache WHERE extractor_version=?',(incremental.EXTRACTOR_VERSION,)))==2


@pytest.mark.parametrize('changed',[{'role_strategy':'relations'},{'exclude_types':['自然人']},
        {'instructions':'新增类型需要业务人员再次确认'},{'baseline_fingerprint':'different'},
        {'target_branch':{'branch_id':'another'}},{'project_id':'second'}])
def test_each_semantic_intent_dimension_changes_cache_key(changed):
    p=piece('李某报警。')
    assert incremental.clue_cache_key(p,config('目标'))!=incremental.clue_cache_key(p,config('目标',**changed))


def test_role_choice_does_not_rewrite_source_or_invent_inheritance(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',empty_model)
    p=piece('李某报警。')
    with brain.using_model(config('报警受理',role_strategy='ask')):ask=brain.joint_extract_segment(p)
    with brain.using_model(config('报警受理',role_strategy='relations')):rel=brain.joint_extract_segment(p)
    assert any(e['name']=='报警人' for e in ask['entities'])
    assert not any(e['name']=='报警人' for e in rel['entities'])
    assert any(e['name']=='自然人' for e in rel['entities'])
    assert not any(r.get('predicate')=='subClassOf' for r in rel['relations'])
    assert all(x['_basis']['kind']=='inference' for x in rel['entities'])
    assert rel['_intent_applied']['omitted_candidates']>=1
    assert p['content']=='李某报警。'


def test_explicit_source_role_not_erased_by_default_role_preference(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',empty_model)
    with brain.using_model(config('目标',role_strategy='relations')):
        out=brain.joint_extract_segment(piece('实体：报警人'))
    assert out['entities'][0]['name']=='报警人'
    assert out['entities'][0]['_basis']['kind']=='source_explicit'


def test_examples_are_not_certified_as_formal_definition():
    p=piece('实体：设备',section='备选方案 / 示例如下')
    with brain.using_model({'base_url':'','model':'','api_key':''}):out=brain.joint_extract_segment(p)
    assert out['entities'][0]['_basis']['kind']=='inference'
    assert any(g.get('category')=='source_status' for g in out['_unresolved'])


def test_model_cannot_promote_its_inference_to_source_fact(monkeypatch):
    def model(*args):
        return {'entities':[{'name':'审核活动','quote':'审核属于某种流程',
            '_basis':{'kind':'source_explicit'}}],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    with brain.using_model(config('流程')):out=brain.joint_extract_segment(piece('审核属于某种流程，但我们尚未定义类型。'))
    assert out['entities'][0]['_basis']['kind']=='inference'
    assert out['entities'][0]['_basis']['method']=='llm'


def test_user_added_fact_cannot_be_fabricated_as_document_quote(monkeypatch):
    def model(*args):
        return {'entities':[{'name':'评分类','quote':'必须设评分类'}],
                'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    with brain.using_model(config('目标',instructions='必须设评分类')):
        out=brain.joint_extract_segment(piece('这份资料讨论质量检查流程，尚未明确其类型。'))
    assert out['entities']==[]
    assert out['_unresolved']


def test_excluded_types_do_not_destroy_source_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr(brain,'query_llm',empty_model);store=Store(tmp_path)
    p=piece('实体：设备、自然人')
    conf=config('设备类型',include_types=['设备'])
    with brain.using_model(conf):out,_=incremental.cached_clues(store,p,conf,brain.joint_extract_segment)
    assert {e['name'] for e in out['entities']}=={'设备'}
    raw=json.loads(store.one('SELECT result FROM extraction_cache WHERE extractor_version=?',(SOURCE_VERSION,))['result'])
    assert {e['name'] for e in raw['definitions']['entities']}=={'设备','自然人'}
    assert out['_intent_applied']['omitted_candidates']==1


def test_requested_semantic_review_without_model_is_not_complete():
    conf=config('类型复核',reading_mode='semantic_review');conf.update(base_url='',model='')
    with brain.using_model(conf):out=brain.joint_extract_segment(piece('实体：设备'))
    assert not out['_complete']
    assert any('未配置模型' in x['reason'] for x in out['_unresolved'])


def workspace(client):
    r=client.post('/api/projects',json={'name':'非规范材料测试'});assert r.status_code==200,r.text
    pid=r.json()['project']['id']
    r=client.post(f'/api/projects/{pid}/documents',files={'file':('材料.txt','实体：设备、自然人'.encode(),'text/plain')})
    assert r.status_code==200,r.text
    r=client.put(f'/api/projects/{pid}/topic',json={'summary':'设备与人员类型','scope':'通用业务类型，不录入实例'})
    assert r.status_code==200,r.text
    return pid


def wait_job(client,pid,jid):
    for _ in range(180):
        state=client.get(f'/api/projects/{pid}/jobs/{jid}').json()['job']
        if state['state'] not in ('queued','running'):return state
        time.sleep(.015)
    raise AssertionError('local job did not finish')


def test_context_api_is_versioned_audited_and_source_independent(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c);before=c.app.state.store.many('SELECT content FROM documents WHERE project_id=?',(pid,))
        r=c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'仅设备','exclude_types':['自然人'],'note':'业务要求不建人员类'})
        assert r.status_code==200,r.text
        state=c.get(f'/api/projects/{pid}/modeling-context').json()
        assert state['context']['version']==1 and state['context']['confirmed']
        assert state['history'][0]['note']=='业务要求不建人员类'
        assert before==c.app.state.store.many('SELECT content FROM documents WHERE project_id=?',(pid,))
        stale=c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'恶意覆盖旧版本'})
        assert stale.status_code==409
        assert c.get(f'/api/projects/{pid}').json()['snapshot']['entities']==[]
        assert any('用户确认建模意图' in row['content'] for row in c.get(f'/api/projects/{pid}').json()['messages'])


def test_cannot_include_and_exclude_same_type(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        r=c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'include_types':['设备'],'exclude_types':['设备']})
        assert r.status_code==422


def test_pipeline_labels_origin_and_preserves_scope(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        assert c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'设备台账','include_types':['设备']}).status_code==200
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=propose');assert r.status_code==202,r.text
        state=wait_job(c,pid,r.json()['job']['id']);assert state['state']=='succeeded',state
        items=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items']
        assert [x['payload']['name'] for x in items]==['设备']
        assert items[0]['basis']['kind']=='source_explicit'
        result=json.loads(state['result'])
        assert result['metrics']['llm_requests']==0 if 'llm_requests' in result['metrics'] else True
        assert result['metrics']['source_cache_misses']>=1
        assert result['omitted_by_user_scope']==1


def test_changed_goal_cannot_bulk_approve_old_suggestions_then_manual_recheck(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        r=c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'设备','code':'device'},
            'evidence':[],'_basis':{'kind':'source_explicit'}})
        assert r.status_code==200,r.text
        qid=r.json()['created'][0]
        first=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items'][0]
        assert first['basis']['kind']=='user_decision'  # API input cannot forge source facts
        r=c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'只构建人员类型'})
        assert r.status_code==200
        stale=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items'][0]
        assert stale['basis']['context_stale'] is True
        r=c.post(f'/api/projects/{pid}/proposals/{qid}/decision',json={'action':'accept'})
        assert r.status_code==409 and '旧的建模意图' in r.text
        view=c.get(f'/api/projects/{pid}').json();assert not view['snapshot']['entities']
        r=c.post(f'/api/projects/{pid}/proposals/{qid}/recheck-intent',json={'expected_revision':view['project']['revision'],'note':'人工决定设备类仍可纳入作为支撑类型'})
        assert r.status_code==200,r.text
        assert c.get(f'/api/projects/{pid}').json()['snapshot']['entities']==[]
        r=c.post(f'/api/projects/{pid}/proposals/{qid}/decision',json={'action':'accept'})
        assert r.status_code==200,r.text
        assert c.get(f'/api/projects/{pid}').json()['snapshot']['entities'][0]['name']=='设备'


def test_context_deletion_and_old_project_data_survive_upgrade(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'通用台账'})
        c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'设备','code':'device'}})
        r=c.get(f'/api/projects/{pid}').json()
        result=c.request('DELETE',f'/api/projects/{pid}',json={'confirm_name':r['project']['name'],'expected_revision':r['project']['revision']})
        assert result.status_code==200,result.text
        assert c.app.state.store.one('SELECT COUNT(*) n FROM modeling_decisions')['n']==0
        assert c.app.state.store.one('SELECT COUNT(*) n FROM proposal_origins')['n']==0


def test_changing_intent_during_model_call_prevents_stale_commit(tmp_path,monkeypatch):
    monkeypatch.setenv('STRUCTURED_EXTRACTION','1')
    entered=threading.Event();release=threading.Event()
    def model(system,user):
        entered.set();assert release.wait(3),'test did not release model'
        return {'entities':[{'name':'事件类型','quote':'事件需要分类'}],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        c.post(f'/api/projects/{pid}/documents',files={'file':('补充.txt','事件需要分类，但如何分类待协商。'.encode(),'text/plain')})
        c.put(f'/api/projects/{pid}/topic',json={'summary':'事件分类','scope':'事件分类'})
        monkeypatch.setattr(c.app.state.model_settings,'effective',lambda:dict(MODEL))
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=propose');assert r.status_code==202,r.text
        assert entered.wait(2)
        try:
            changed=c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'不处理事件','exclude_types':['事件类型']})
            assert changed.status_code==200,changed.text
        finally:release.set()
        state=wait_job(c,pid,r.json()['job']['id'])
        assert state['state'] in ('failed','partial','interrupted'),state
        items=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items']
        assert not any(x['payload']['name']=='事件类型' for x in items)


def test_reading_boundaries_survive_candidate_pipeline_and_mark_partial(tmp_path,monkeypatch):
    monkeypatch.setenv('STRUCTURED_EXTRACTION','1')
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        d=Document();d.add_paragraph('实体：部件')
        png=io.BytesIO();Image.new('RGB',(20,20),'white').save(png,format='PNG');png.seek(0)
        d.add_picture(png)
        raw=io.BytesIO();d.save(raw)
        r=c.post(f'/api/projects/{pid}/documents',files={'file':('带图片.docx',raw.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        assert r.status_code==200,r.text
        c.put(f'/api/projects/{pid}/topic',json={'summary':'设备类型','scope':'设备与部件'})
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=propose');assert r.status_code==202,r.text
        state=wait_job(c,pid,r.json()['job']['id'])
        assert state['state']=='partial',state
        result=json.loads(state['result'])
        assert result['source_parts_complete'] is False and result['reading_boundaries']>=1
        items=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items']
        assert any(x['payload']['name']=='部件' for x in items)
        gaps=c.get(f'/api/projects/{pid}/gaps?status=all').json()['items']
        assert any(x['kind']=='source_coverage' and '图片' in x['reason'] for x in gaps)


def test_synchronous_propose_respects_explicit_user_scope(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        assert c.put(f'/api/projects/{pid}/modeling-context',json={
            'expected_version':0,'exclude_types':['自然人'],'goal':'设备台账'}).status_code==200
        r=c.post(f'/api/projects/{pid}/propose');assert r.status_code==200,r.text
        items=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items']
        assert any(x['payload']['name']=='设备' for x in items)
        assert not any(x['payload']['name']=='自然人' for x in items)
        assert r.json()['omitted_by_user_scope']


def test_word_visual_bold_is_not_assumed_heading(tmp_path):
    d=Document();p=d.add_paragraph();p.add_run('看起来像标题但没有声明归属').bold=True
    create_table(d,[['字段名称','数据类型'],['状态','string']])
    path=tmp_path/'自由格式.docx';d.save(path)
    result=extract(path)
    assert result['chunks'][0]['kind']=='prose'
    table=next(c for c in result['chunks'] if c['kind']=='table')
    assert not table['section']
    found=deterministic_discover({**table,'content':table['text']})
    assert found['attributes']==[] and found['_semantic_text']


def test_cache_policy_change_during_gap_retry_preserves_issue_identity(tmp_path):
    from app.gap_state import record_gap
    from app.store import now,uid,j
    db=Store(tmp_path);pid=uid();did=uid()
    p=piece('需要确认的字段',document_id=did,project_id=pid,ordinal=0)
    with db.tx() as c:
        c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',(pid,'测试',now(),now()))
        c.execute('INSERT INTO documents(id,project_id,filename,sha256,bytes,path,content,chunks,warnings,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (did,pid,'file.txt',p['content_hash'],20,'',p['content'],'[]','[]',now()))
        issue=record_gap(c,pid,'ENTITY',1,'discovery',{'source_excerpt':'字段'},[],'类型归属不明',piece=p,cache_key='old')
        c.execute("UPDATE extraction_gaps SET status='retrying' WHERE id=?",(issue,))
        p['_retry_gap_id']=issue
        same=record_gap(c,pid,'ENTITY',1,'discovery',{'source_excerpt':'字段'},[],'类型归属不明',piece=p,cache_key='new-intent')
        different=record_gap(c,pid,'ENTITY',1,'discovery',{'source_excerpt':'字段'},[],'另一个问题',piece=p,cache_key='new-intent')
    assert same==issue and different!=issue
    assert db.one('SELECT cache_key FROM extraction_gaps WHERE id=?',(issue,))['cache_key']=='new-intent'


def test_source_conflicting_types_preserve_both_claims_not_last_answer(tmp_path):
    # The parser must preserve conflicting source definitions. A later review
    # must decide what the user intends; source cache may not rewrite the text.
    texts=['属性：设备.编号：integer','属性：设备.编号：string']
    store=Store(tmp_path);collected=[]
    conf={**config('核查冲突'),'base_url':'','model':''}
    for text in texts:
        with brain.using_model(conf):
            out,_=incremental.cached_clues(store,piece(text),conf,brain.joint_extract_segment)
        collected+=out['attributes']
    assert {v['datatype'] for v in collected}=={'integer','string'}
    assert {v['quote'] for v in collected}==set(texts)


def test_pre_upgrade_unclassified_candidate_requires_review_after_new_intent(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        pid=workspace(c)
        r=c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'设备','code':'device'}})
        qid=r.json()['created'][0]
        with c.app.state.store.tx() as db:
            db.execute('DELETE FROM proposal_origins WHERE proposal_id=?',(qid,))  # legacy saved candidate
        assert c.put(f'/api/projects/{pid}/modeling-context',json={'expected_version':0,'goal':'只建人员'}).status_code==200
        item=c.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items'][0]
        assert item['basis']['kind']=='legacy' and item['basis']['context_stale']
        assert c.post(f'/api/projects/{pid}/proposals/{qid}/decision',json={'action':'accept'}).status_code==409
        assert c.get(f'/api/projects/{pid}').json()['snapshot']['entities']==[]


def test_instance_mention_in_explicit_table_is_still_inferred_type_not_source_definition():
    text='| 实体名称 | 描述 |\n|---|---|\n| 李某 | 参与人 |'
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        out=brain.joint_extract_segment(piece(text,kind='table',section='材料清单'))
    entity=next(x for x in out['entities'] if x['name']=='自然人')
    assert entity['_basis']['kind']=='inference'
    assert entity['_basis']['method']=='instance_to_type_abstraction'
    assert '李某' in entity['quote'] and entity['quote'] in text
