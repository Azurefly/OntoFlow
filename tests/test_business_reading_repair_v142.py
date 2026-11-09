"""Generic regression gates; no BC/real public-document answers are embedded.

Frozen internet bytes are tested separately by scripts.repair_business_gate.
Here the assertions cover invariants, not the number of concepts in a project.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from app import brain,incremental
from app.extraction import extract
from app.main import create_app
from app.ontology_abstraction import discover_narrative_types,normalize_clues
from app.semantic_units import coalesce_prose,quote_locations


def part(text, **extra):
    return {'document_id':'d','project_id':'p','content':text,'content_hash':incremental.sha(text),
            'kind':'prose','section':'业务说明','locator':'原文1',**extra}


def test_occurrences_unique_cache_identity_still_shared():
    chunks=[{'kind':'prose','text':'此项还需审批。','locator':'HTML 行1'} for _ in range(40)]
    doc={'id':'d','project_id':'p','chunks':chunks}
    items=list(incremental.segments([doc]))
    assert len({x['id'] for x in items})==len(chunks)
    assert len({incremental.clue_cache_key(x,{}) for x in items})==1
    assert [x['id'] for x in items]==[x['id'] for x in incremental.segments([doc])]


def test_repeated_html_upload_real_api_succeeds(tmp_path,monkeypatch):
    monkeypatch.setenv('ASYNC_PARSE','false');monkeypatch.setenv('ONTOFLOW_DEMO_MODE','true')
    raw=('<html><body>'+''.join('<p>申请人提交申请材料，由审核人员进行审核。</p>' for _ in range(550))+'</body></html>').encode()
    with TestClient(create_app(tmp_path)) as client:
        pid=client.post('/api/projects',json={'name':'重复条款测试'}).json()['project']['id']
        res=client.post(f'/api/projects/{pid}/documents',files={'file':('terms.html',raw,'text/html')})
        assert res.status_code==200,res.text


def test_semantic_units_keep_every_source_occurrence_and_boundary():
    original=[{'text':f'第{i}条：独立业务条款。','kind':'prose','locator':'HTML 行1',
               'source_part':'html','section':'受理','source_order':i} for i in range(350)]
    original.insert(150,{'kind':'table','text':'|栏目|内容|\n|---|---|\n|甲|乙|','locator':'表1','section':'受理'})
    merged=coalesce_prose(original,max_chars=900,max_members=40)
    assert len(merged)<30
    assert [x['text'] for x in merged if x['kind']=='table']==[original[150]['text']]
    recovered=[]
    for x in merged:
        if x.get('source_spans'):
            recovered.extend(x['text'][s['start']:s['end']] for s in x['source_spans'])
            assert len(x['text'])<=900 and len(x['source_spans'])<=40
        else:recovered.append(x['text'])
    assert recovered==[x['text'] for x in original]


def test_records_and_changed_sections_are_not_merged():
    units=[{'text':'同样描述','kind':'prose','locator':f'$[{i}]','source_part':'',
            'section':'业务记录'} for i in range(30)]
    assert coalesce_prose(units)==units
    html=[dict(x,source_part='html',section='主题A' if i<15 else '主题B') for i,x in enumerate(units)]
    merged=coalesce_prose(html)
    assert len(merged)==2 and [x['section'] for x in merged]==['主题A','主题B']


def test_quote_resolves_to_each_original_occurrence():
    units=[{'text':f'编号{i}：需审批。','kind':'prose','locator':f'节点{i}',
            'source_part':'html','section':'正文','source_order':i} for i in range(9)]
    u=coalesce_prose(units)[0]
    piece=part(u['text'],source_spans=u['source_spans'])
    found=quote_locations(piece,'需审批。')
    assert len(found)==9 and len({v['locator'] for v in found})==9
    quoted=quote_locations(piece,'编号1：需审批。\n\n编号2：需审批。')
    assert len(quoted)==2 and quoted[0]['quote']=='编号1：需审批。'


@pytest.mark.parametrize('text',[
    '入侵检测系统应对非法访问进行报警。','数据库审计与响应报警。',
    '监控设备报警后，李某检查线路。','报警阈值应由管理员配置。',
    '当温度异常时进行报警，并记录告警日志。',
])
def test_alarm_is_not_assumed_to_be_police(text):
    out=discover_narrative_types(text)
    assert not any(e['name'] in ('报警事件','报警人','报案人') for e in out['entities'])
    bad={'entities':[{'name':'报警事件','description':'向警方报警或报案的事件类型。','quote':'报警'}],
         'attributes':[],'relations':[]}
    assert not normalize_clues(bad,text)['entities']


@pytest.mark.parametrize('text',['李某报警。','李某向派出所报案。','王某拨打110。','李某到了派出所。随后其报警。'])
def test_explicit_police_context_still_supported(text):
    out=discover_narrative_types(text)
    assert any(e['name']=='报警事件' for e in out['entities'])
    assert all(v['quote'] in text for v in out['entities'])


@pytest.mark.parametrize('text',[
    '服务费支付方式：现金、支票或转账。','费用可以采用转账、现金方式支付。',
    '申明在投标过程中遵守采购要求。',
])
def test_options_and_declarations_do_not_become_case_facts(text):
    out=discover_narrative_types(text)
    assert not out['entities'] and not out['relations']


@pytest.mark.parametrize('text',['申请人提交材料，审核部门作出决定。','设备故障后应更换部件。','This document describes the procurement process.'])
def test_unmodeled_prose_cannot_be_cached_as_semantically_complete(text):
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        out=brain.joint_extract_segment(part(text))
    assert out['_complete'] is False and out['_state']=='awaiting_model'
    assert out['_coverage']['semantic_status']=='awaiting_model'


def test_waiting_model_checkpoint_can_resume(monkeypatch):
    p=part('申请人提交材料，审核部门作出决定。')
    with brain.using_model({'base_url':'','model':'','api_key':''}):first=brain.joint_extract_segment(p)
    calls=[]
    def actual_boundary(s,u):
        calls.append(u)
        return {'entities':[{'name':'申请人','code':'applicant','quote':'申请人'}],
                'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',actual_boundary)
    with brain.using_model({'base_url':'http://model.invalid/v1','model':'test','api_key':''}):
        second=brain.joint_extract_segment(p,checkpoint=first)
    assert calls and second['_complete']
    assert not any(g.get('category')=='awaiting_model' for g in second['_unresolved'])


def _pdf(path,password=None):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    w=PdfWriter();p=w.add_blank_page(300,180)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 130 Td (Procurement service terms) Tj ET')
    p[NameObject('/Contents')]=w._add_object(content)
    if password is not None:w.encrypt(password,owner_password='test-only-owner')
    w.write(path)


def test_empty_password_pdf_is_read_only_and_actual_password_still_required(tmp_path):
    empty=tmp_path/'empty-open.pdf';_pdf(empty,'');original=empty.read_bytes()
    out=extract(empty)
    assert 'Procurement service terms' in out['content']
    assert empty.read_bytes()==original
    private=tmp_path/'password.pdf';_pdf(private,'not-empty-test-only')
    with pytest.raises(Exception,match='口令'):extract(private)


def test_strict_docx_transitional_text_and_order_match(tmp_path):
    from docx import Document
    from app.ooxml_compat import NAMESPACES
    doc=Document();doc.add_heading('馆藏品的属性定义',level=1)
    t=doc.add_table(rows=2,cols=2)
    for cell,text in zip(t.rows[0].cells,['属性名称','数据类型']):cell.text=text
    for cell,text in zip(t.rows[1].cells,['保管状态','string']):cell.text=text
    doc.add_paragraph('馆藏品的属性需要人工审核。')
    trans=tmp_path/'trans.docx';doc.save(trans)
    strict=tmp_path/'strict.docx'
    with ZipFile(trans) as source,ZipFile(strict,'w') as target:
        for item in source.infolist():
            data=source.read(item.filename)
            if item.filename.endswith(('.xml','.rels')):
                for old,new in NAMESPACES.items():data=data.replace(new.encode(),old.encode())
            target.writestr(item,data)
    original=strict.read_bytes();a=extract(trans);b=extract(strict)
    assert a['content']==b['content']
    assert [u['kind'] for u in a['chunks']]==[u['kind'] for u in b['chunks']]
    assert strict.read_bytes()==original


def test_forms_are_read_not_only_page_text(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    path=tmp_path/'form.pdf';c=Canvas(str(path))
    c.drawString(40,750,'Service intake')
    c.acroForm.textfield(name='request_id',value='REQ-42',x=40,y=680,width=200,height=25)
    c.acroForm.checkbox(name='approved',checked=True,x=40,y=620)
    c.save()
    original=path.read_bytes();out=extract(path)
    assert 'REQ-42' in out['content'] and 'approved' in out['content']
    assert any(u.get('source_part')=='pdf_form' for u in out['chunks'])
    assert path.read_bytes()==original


def test_first_checkpoint_never_marks_unmodeled_prose_complete():
    captured=[]
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        result=brain.joint_extract_segment(part('采购人审核供应商提供的履约材料。'),on_checkpoint=captured.append)
    assert captured and not result['_complete']
    assert all(not record['_complete'] and record['_state']=='awaiting_model' for record in captured)
