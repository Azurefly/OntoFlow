"""Cross-format, source-completeness and adversarial regressions.

All samples are generated from unrelated synthetic library data. Expected facts
are fixed in this test, never passed into extraction code; no model/network calls.
The suite verifies source reading and type candidates, not model intelligence.
"""
from __future__ import annotations
import csv
import io
import json
import textwrap
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile,ZIP_DEFLATED
import pytest
from fastapi.testclient import TestClient
from app import brain,incremental
from app.extraction import extract,ExtractionError
from app.format_registry import capabilities
from app.main import create_app
from app.structured_discovery import tabular_chunks
from app.text_io import decode_text

TEXT='实体：馆藏品、入藏事件\n属性：馆藏品.保管状态：string\n关系：馆藏品 participates_in 入藏事件'
DEFS={'name':'馆藏业务','entities':[{'name':'馆藏品','kind':'class'},{'name':'入藏事件','kind':'class'}],
      'attributes':[{'owner':'馆藏品','name':'保管状态','dataType':'string','code':'storage_state'}],
      'relations':[{'source':'馆藏品','target':'入藏事件','code':'participates_in','name':'参与入藏'}]}
EXPECTED={('e','馆藏品'),('e','入藏事件'),('a','馆藏品','保管状态','string'),('r','馆藏品','participates_in','入藏事件')}
TABLES=[('实体定义',[['实体名称','实体编号'],['馆藏品','collection_item'],['入藏事件','accession_event']]),
        ('馆藏品的属性定义',[['属性名称','所属实体','数据类型'],['保管状态','馆藏品','string']]),
        ('关系定义',[['源实体','关系名称','目标实体'],['馆藏品','participates_in','入藏事件']])]


def type_facts(parsed):
    result=set()
    doc={'id':'test-source','project_id':'test-workspace','filename':'unseen-input', 'chunks':parsed['chunks']}
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        for chunk in incremental.segments([doc]):
            clues=brain.joint_extract_segment(chunk)
            result.update(('e',x['name']) for x in clues['entities'])
            result.update(('a',x['owner'],x['name'],x.get('datatype')) for x in clues['attributes'])
            result.update(('r',x['source'],x['predicate'],x['target']) for x in clues['relations'])
    return result


@pytest.mark.parametrize('extension',['txt','log','sql','rst','md','markdown','json','jsonl','ndjson','yaml','yml','xml','html','htm','eml'])
def test_identical_schema_facts_across_text_formats(tmp_path,monkeypatch,extension):
    def forbid(*args,**kwargs):raise AssertionError('Source structure must not invoke LLM here')
    monkeypatch.setattr(brain,'query_llm',forbid)
    path=tmp_path/('unseen.'+extension)
    if extension=='json':source=json.dumps(DEFS,ensure_ascii=False)
    elif extension in ('yaml','yml'):
        import yaml
        source=yaml.safe_dump(DEFS,allow_unicode=True,sort_keys=False)
    elif extension in ('jsonl','ndjson'):
        source=json.dumps({'描述':TEXT},ensure_ascii=False)+'\n'
    elif extension=='xml':source='<document>'+''.join('<p>'+escape(line)+'</p>' for line in TEXT.splitlines())+'</document>'
    elif extension in ('html','htm'):source='<html><body><h1>馆藏业务</h1>'+''.join('<p>'+escape(line)+'</p>' for line in TEXT.splitlines())+'</body></html>'
    elif extension=='eml':
        message=EmailMessage();message['Subject']='馆藏设计';message['From']='test@example.invalid';message.set_content(TEXT)
        path.write_bytes(message.as_bytes());source=None
    else:source=TEXT
    if source is not None:path.write_text(source,encoding='utf-8')
    parsed=extract(path)
    assert EXPECTED <= type_facts(parsed),(extension,type_facts(parsed))
    assert all(c['locator'] for c in parsed['chunks'])
    assert not any(x[1] in ('example','test','李某','500元') for x in type_facts(parsed) if x[0]=='e')


@pytest.mark.parametrize('extension,delimiter',[('csv',','),('tsv','\t'),('csv',';')])
def test_three_tables_across_delimiters(tmp_path,extension,delimiter):
    result=set()
    for i,(_,rows) in enumerate(TABLES):
        f=tmp_path/f'{i}.{extension}'
        with f.open('w',encoding='utf-8',newline='') as stream:csv.writer(stream,delimiter=delimiter).writerows(rows)
        result|=type_facts(extract(f))
    assert result==EXPECTED


def _xlsx_xml_fixture(path):
    """Minimal OOXML test file written with stdlib, no spreadsheet editor needed."""
    names=[name for name,_ in TABLES]
    xml=lambda s:s.encode('utf-8')
    types=['<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'%i for i in range(1,4)]
    with ZipFile(path,'w',ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml',xml('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join(types)+'</Types>'))
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml',xml('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i,n in enumerate(names,1))+'</sheets></workbook>'))
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,4))+'</Relationships>')
        for index,(_,rows) in enumerate(TABLES,1):
            body=[]
            for rn,row in enumerate(rows,1):
                body.append(f'<row r="{rn}">'+''.join(f'<c r="{chr(65+c)}{rn}" t="inlineStr"><is><t>{escape(val)}</t></is></c>' for c,val in enumerate(row))+'</row>')
            archive.writestr(f'xl/worksheets/sheet{index}.xml',xml('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(body)+'</sheetData></worksheet>'))


@pytest.mark.parametrize('extension',['docx','xlsx','pptx'])
def test_table_facts_across_office_formats(tmp_path,extension):
    path=tmp_path/('source.'+extension)
    if extension=='docx':
        from docx import Document
        doc=Document()
        for name,rows in TABLES:
            doc.add_heading(name,1);t=doc.add_table(rows=0,cols=len(rows[0]))
            for row in rows:
                cells=t.add_row().cells
                for cell,value in zip(cells,row):cell.text=value
        doc.save(path)
    elif extension=='xlsx':_xlsx_xml_fixture(path)
    else:
        from pptx import Presentation
        from pptx.util import Inches
        doc=Presentation()
        for name,rows in TABLES:
            slide=doc.slides.add_slide(doc.slide_layouts[5]);slide.shapes.title.text=name
            tab=slide.shapes.add_table(len(rows),len(rows[0]),Inches(.4),Inches(1.4),Inches(9),Inches(2)).table
            for i,row in enumerate(rows):
                for j,value in enumerate(row):tab.cell(i,j).text=value
        doc.save(path)
    parsed=extract(path)
    assert type_facts(parsed)==EXPECTED
    assert any(c['kind']=='table' for c in parsed['chunks'])


@pytest.mark.parametrize('encoding',['utf-8','utf-8-sig','utf-16','utf-32','gb18030'])
def test_chinese_encoding_is_preserved(tmp_path,encoding):
    path=tmp_path/'enc.txt';path.write_bytes(TEXT.encode(encoding))
    parsed=extract(path)
    assert EXPECTED<=type_facts(parsed)
    assert parsed['chunks'][0]['encoding']


def test_gb_even_bytes_never_decode_as_arbitrary_utf16():
    data='实体：自然人'.encode('gb18030')
    assert len(data)%2==0
    assert decode_text(data).text=='实体：自然人'
    assert decode_text(data).encoding=='gb18030'


@pytest.mark.parametrize('extension,payload',[('json','{"a":1,"a":2}'),('json','{"a":NaN}'),('json','{"a":1e999}'),
    ('yaml','a: 1\na: 2'),('yaml','a: .nan'),('yaml','a: &loop [*loop]'),('yaml','!!python/object/apply:os.system ["exit 0"]'),
    ('xml','<!DOCTYPE d [<!ENTITY e SYSTEM "file:///etc/passwd">]><d>&e;</d>')])
def test_unsafe_or_ambiguous_structured_input_is_not_silent_success(tmp_path,extension,payload):
    path=tmp_path/('bad.'+extension);path.write_text(payload,encoding='utf-8')
    with pytest.raises(ExtractionError):extract(path)


def test_yaml_reads_every_document_and_final_record(tmp_path):
    p=tmp_path/'multi.yaml';p.write_text('描述: "实体：馆藏品"\n---\n描述: "实体：末尾定义"\n',encoding='utf-8')
    parsed=extract(p)
    assert ('e','末尾定义') in type_facts(parsed)
    locs=[c['locator'] for c in parsed['chunks']]
    assert any('文档 1' in x for x in locs) and any('文档 2' in x for x in locs)
    assert len(locs)==len(set(locs))


def test_jsonl_errors_report_actual_line(tmp_path):
    p=tmp_path/'bad.ndjson';p.write_text('\n{"描述":"有效"}\n{invalid}\n')
    with pytest.raises(ExtractionError,match='第 3 行'):extract(p)


def test_arbitrary_json_is_retained_for_semantics_not_discarded(tmp_path):
    p=tmp_path/'misc.json';p.write_text('{"meta":{"note":"没有约定字段"},"enabled":false,"count":0}',encoding='utf-8')
    out=extract(p)
    assert '没有约定字段' in out['content'] and 'false' in out['content'] and '0' in out['content']
    assert out['warnings'] and out['reading_status']=='needs_review'
    assert not type_facts(out)


def test_html_heading_table_and_mixed_text_order(tmp_path):
    p=tmp_path/'a.html';p.write_text('''<html><head><script>实体：假实体</script></head><body>
<h1>设备的属性定义</h1><table><tr><th>属性名称</th><th>数据类型</th></tr><tr><td>设备编号</td><td>string</td></tr></table>
<h1>自然人的属性定义</h1><table><tr><th>属性名称</th><th>数据类型</th></tr><tr><td>姓名</td><td>string</td></tr></table>
<p>尾部<span>补充说明</span>。</p></body></html>''',encoding='utf-8')
    out=extract(p)
    assert [x['kind'] for x in out['chunks']]==['heading','table','heading','table','prose']
    assert ('a','设备','设备编号','string') in type_facts(out)
    assert ('a','自然人','姓名','string') in type_facts(out)
    assert '假实体' not in out['content'] and '尾部补充说明。' in out['content']


def test_html_merged_image_and_external_content_get_boundaries(tmp_path):
    p=tmp_path/'complex.html';p.write_text('<h1>未定类型</h1><table><tr><th colspan="2">说明</th></tr><tr><td>数据</td><td>尾列不能丢失</td></tr></table><p><img src="http://invalid.example/scan.png" alt="扫描说明"></p><iframe src="http://invalid.example"></iframe>',encoding='utf-8')
    out=extract(p)
    assert '尾列不能丢失' in out['content'] and '扫描说明' in out['content']
    assert any(c['kind']=='ambiguous_table' for c in out['chunks'])
    assert any('图片' in w for w in out['warnings']) and any('嵌入' in w for w in out['warnings'])


def test_xml_attributes_tail_nested_attributes_and_inline_content(tmp_path):
    p=tmp_path/'a.xml';p.write_text('<root id="r1"><p code="c1">实体：<b role="label">馆藏品</b></p><item enabled="false">前<em><span tag="deep">中</span></em>尾部文本</item></root>',encoding='utf-8')
    out=extract(p)
    for marker in ('id="r1"','code="c1"','role="label"','enabled="false"','tag="deep"','尾部文本'):
        assert marker in out['content']
    assert ('e','馆藏品') in type_facts(out)
    assert all('XPath' in x['locator'] for x in out['chunks'])


def test_table_zero_false_and_excess_columns_are_not_lost():
    out=tabular_chunks([(1,['名称','值']),(2,['计数',0]),(3,['启用',False]),(4,['长行',1,'尾部字段'])],'未知表')
    text='\n'.join(c['text'] for c in out)
    assert '0' in text and 'False' in text and '尾部字段' in text
    assert all(c['kind']=='ambiguous_table' for c in out)


def test_large_csv_quoted_multiline_cell_keeps_tail(tmp_path):
    p=tmp_path/'long.csv'
    stream=io.StringIO();csv.writer(stream).writerows([['属性名称','备注'],['测试','前\n'+('说明'*70000)+'末尾保留']])
    p.write_text(stream.getvalue(),encoding='utf-8')
    out=extract(p)
    assert '末尾保留' in out['content']


def test_eml_body_attachments_and_no_double_alternative(tmp_path):
    message=EmailMessage();message['Subject']='场景说明';message.set_content(TEXT)
    message.add_alternative('<p>HTML替代正文不重复</p>',subtype='html')
    message.add_attachment(b'not parsed',maintype='application',subtype='octet-stream',filename='data.bin')
    p=tmp_path/'a.eml';p.write_bytes(message.as_bytes());out=extract(p)
    assert EXPECTED<=type_facts(out)
    assert 'HTML替代正文不重复' not in out['content']
    assert any('data.bin' in x for x in out['warnings'])


def test_pptx_group_text_notes_and_picture_are_accounted(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    from PIL import Image
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[5]);slide.shapes.title.text='测试材料'
    group=slide.shapes.add_group_shape();box=group.shapes.add_textbox(0,0,Inches(3),Inches(1));box.text='实体：组合中的类型'
    slide.notes_slide.notes_text_frame.text='备注正文不能丢失'
    image=tmp_path/'tiny.png';Image.new('RGB',(5,5),'white').save(image)
    slide.shapes.add_picture(str(image),Inches(4),Inches(1),Inches(.2),Inches(.2))
    p=tmp_path/'group.pptx';deck.save(p);out=extract(p)
    assert '组合中的类型' in out['content'] and '备注正文不能丢失' in out['content']
    assert any('图片' in w for w in out['warnings'])
    assert any(c['source_part']=='speaker_notes' for c in out['chunks'])


def test_pdf_mixed_text_blank_page_does_not_claim_complete(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    p=tmp_path/'mixed.pdf';doc=Canvas(str(p));doc.drawString(50,750,'Evidence on first page');doc.showPage();doc.showPage();doc.save()
    out=extract(p)
    assert 'Evidence on first page' in out['content']
    assert any('第 2 页' in w and '没有可读' in w for w in out['warnings'])
    assert out['reading_status']=='needs_review'


def test_formats_api_separates_available_from_optional(tmp_path,monkeypatch):
    import app.format_registry as registry
    original=registry._has
    monkeypatch.setattr(registry,'_has',lambda name:False if name=='docling' else original(name))
    with TestClient(create_app(tmp_path/'api',testing=True)) as client:
        data=client.get('/api/formats').json()
        assert '.yaml' in data['extensions'] and '.docx' in data['extensions']
        assert '.doc' not in data['extensions'] and '.doc' in data['known_extensions']
        pid=client.post('/api/projects',json={'name':'类型检查'}).json()['project']['id']
        r=client.post(f'/api/projects/{pid}/documents',files={'file':('old.doc',b'bad','application/msword')})
        assert r.status_code==415 and '另存' in r.text
        assert not list((tmp_path/'api'/'uploads').glob('*'))


@pytest.mark.parametrize('extension,content',[('yaml','描述: "实体：馆藏品"'),('xml','<p>实体：馆藏品</p>'),('html','<h1>设计</h1><p>实体：馆藏品</p>'),('csv','实体名称,实体编号\n馆藏品,item')])
def test_api_upload_and_read_boundaries(tmp_path,extension,content):
    with TestClient(create_app(tmp_path/'api',testing=True)) as client:
        pid=client.post('/api/projects',json={'name':'混合资料'}).json()['project']['id']
        r=client.post(f'/api/projects/{pid}/documents',files={'file':('source.'+extension,content.encode(),'text/plain')})
        assert r.status_code==200,r.text
        data=client.get(f'/api/projects/{pid}/understanding').json()['materials']['documents'][0]
        assert data['units']>0 and data['readers'] and data['encodings']


def _rewrite_zip(path,fn):
    with ZipFile(path) as z:items={n:z.read(n) for n in z.namelist()}
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for name,data in items.items():z.writestr(name,fn(name,data))


def test_xlsx_formula_missing_cache_hidden_sheet_and_merge_are_explicit(tmp_path):
    path=tmp_path/'edge.xlsx';_xlsx_xml_fixture(path)
    def alter(name,data):
        text=data.decode('utf-8')
        if name=='xl/worksheets/sheet1.xml':
            text=text.replace('</sheetData>', '<row r="4"><c r="A4" t="inlineStr"><is><t>计数</t></is></c><c r="B4" t="n"><v>0</v></c></row><row r="5"><c r="A5" t="inlineStr"><is><t>真假</t></is></c><c r="B5" t="b"><v>0</v></c></row><row r="6"><c r="A6"><f>SUM(1,2)</f><v/></c></row></sheetData><mergeCells count="1"><mergeCell ref="A2:B2"/></mergeCells>')
        if name=='xl/workbook.xml':text=text.replace('sheetId="2"','sheetId="2" state="hidden"')
        return text.encode()
    _rewrite_zip(path,alter)
    out=extract(path)
    assert '| 计数 | 0 |' in out['content'] and '| 真假 | False |' in out['content']
    assert '=SUM(1,2)' in out['content']
    assert all(any(word in w for w in out['warnings']) for word in ('缓存结果','合并','隐藏'))
    assert any(c['kind']=='ambiguous_table' for c in out['chunks'])


def test_cjk_pdf_text_is_available_as_source(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    p=tmp_path/'chinese.pdf';doc=Canvas(str(p));doc.setFont('STSong-Light',12)
    for i,line in enumerate(TEXT.splitlines()):doc.drawString(50,750-i*25,line)
    doc.save();parsed=extract(p)
    assert EXPECTED<=type_facts(parsed)
    assert parsed['warnings']  # text layer != layout/semantic completeness


def test_pdf_with_no_text_fails_explicitly_without_running_ocr(tmp_path):
    from pypdf import PdfWriter
    writer=PdfWriter();writer.add_blank_page(100,100);p=tmp_path/'empty.pdf'
    with p.open('wb') as f:writer.write(f)
    with pytest.raises(ExtractionError,match='OCR'):extract(p)


def test_semantic_route_receives_ambiguous_html_and_user_goal(tmp_path,monkeypatch):
    from app import modeling
    p=tmp_path/'ambiguous.html';p.write_text('<h1>一种可能的设备模型</h1><p>这里只说明审批环节的责任，不确定应作为角色还是独立类型。</p><table><tr><td colspan="2">建议内容</td></tr><tr><td>审核人</td><td>经办</td></tr></table>',encoding='utf-8')
    parsed=extract(p);seen=[]
    def model(system,user):
        seen.append(user);return {'entities':[],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    spec={**modeling.DEFAULT,'project_id':'p','goal':'仅建立设备类型，人员角色先询问','confirmed_scope':'设备','baseline_fingerprint':'test'}
    spec.update(fingerprint=modeling.digest(spec),decision_version=1)
    document={'id':'d','project_id':'p','filename':p.name,'chunks':parsed['chunks']}
    with brain.using_model({'base_url':'https://invalid.example','model':'mock','api_key':'','interpretation':spec}):
        outputs=[brain.joint_extract_segment(piece) for piece in incremental.segments([document])]
    assert seen and any('审核人' in x for x in seen)
    assert any('仅建立设备类型' in x for x in seen)
    assert not any(o['entities'] for o in outputs)  # no silent class from merged header


def test_invalid_suffix_or_binary_cannot_pass_as_plaintext(tmp_path):
    p=tmp_path/'fake.txt';p.write_bytes(b'\x00\x01\x02binary')
    with pytest.raises(ExtractionError):extract(p)
    p=tmp_path/'fake.xlsx';p.write_text('not an Office ZIP')
    with pytest.raises(ExtractionError):extract(p)


def test_missing_dependency_does_not_create_queued_document(tmp_path,monkeypatch):
    import app.format_registry as registry
    original=registry._has
    monkeypatch.setattr(registry,'_has',lambda name:False if name=='yaml' else original(name))
    monkeypatch.setenv('ASYNC_PARSE','1')
    with TestClient(create_app(tmp_path/'api',testing=True)) as c:
        pid=c.post('/api/projects',json={'name':'未安装组件'}).json()['project']['id']
        r=c.post(f'/api/projects/{pid}/documents',files={'file':('a.yaml',b'a: 1','text/plain')})
        assert r.status_code==415 and '依赖' in r.text
        assert c.app.state.store.one('SELECT COUNT(*) n FROM documents')['n']==0
        assert c.app.state.store.one('SELECT COUNT(*) n FROM workflow_jobs')['n']==0


def test_long_jsonl_all_narratives_include_last_record(tmp_path):
    p=tmp_path/'many.ndjson';p.write_text('\n'.join(json.dumps({'描述':'实体：末尾才出现的类型' if i==124 else '资料记录'},ensure_ascii=False) for i in range(125)),encoding='utf-8')
    out=extract(p)
    assert sum(c['kind']=='prose' for c in out['chunks'])==125
    assert ('e','末尾才出现的类型') in type_facts(out)
    last=next(c for c in out['chunks'] if '末尾才出现的类型'==c['text'].replace('实体：',''))
    assert last.get('source_line')==125


@pytest.mark.parametrize('extension',['json','yaml'])
def test_record_fields_are_inferred_not_claimed_as_explicit_ontology(tmp_path,extension):
    data={'书籍记录':[{'名称':'手册A','已入库':False}]}
    path=tmp_path/('data.'+extension)
    if extension=='json':path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    else:
        import yaml
        path.write_text(yaml.safe_dump(data,allow_unicode=True),encoding='utf-8')
    parsed=extract(path);doc={'id':'d','project_id':'p','filename':path.name,'chunks':parsed['chunks']}
    from app.source_understanding import read_source
    candidates=[]
    for piece in incremental.segments([doc]):
        evidence=read_source(piece)['definitions']
        candidates.extend(evidence['entities']+evidence['attributes'])
    assert candidates and all(c['_basis']['kind']=='inference' for c in candidates)


def test_async_partial_read_reports_warnings_in_right_assistant(tmp_path,monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    monkeypatch.setenv('ASYNC_PARSE','1')
    p=tmp_path/'p.pdf';cv=Canvas(str(p));cv.drawString(30,700,'text');cv.showPage();cv.showPage();cv.save()
    with TestClient(create_app(tmp_path/'api',testing=True)) as c:
        pid=c.post('/api/projects',json={'name':'PDF阅读边界'}).json()['project']['id']
        uploaded=c.post(f'/api/projects/{pid}/documents',files={'file':('p.pdf',p.read_bytes(),'application/pdf')}).json()
        c.app.state.run_job(pid,uploaded['job_id'],'parse',uploaded['document']['id'],uploaded['document']['id'],{},'test')
        job=c.app.state.store.one('SELECT * FROM workflow_jobs WHERE id=?',(uploaded['job_id'],))
        assert job['state']=='partial',job
        result=json.loads(job['result'])
        assert any('第 2 页' in warning for warning in result['reading_warnings'])
        event=c.app.state.store.one("SELECT content FROM job_progress_events WHERE job_id=? AND phase='partial'",(uploaded['job_id'],))
        assert event and '需核查' in event['content']
