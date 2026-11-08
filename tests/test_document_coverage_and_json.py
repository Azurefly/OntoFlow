"""Independent regressions for document truncation and malformed model JSON."""
import io
import json
import re
import sqlite3

import pytest
from docx import Document
from openpyxl import Workbook
from fastapi.testclient import TestClient

from app import brain
from app.extraction import extract
from app.main import create_app


def test_long_document_all_chapters_are_sent_and_coverage_is_visible(monkeypatch):
    chapters='\n'.join(f'第{i}章 {i}号关键内容\n' + ('详细业务事实。'*520)
                      for i in range(1, 12))
    doc={'id':'file123','filename':'manual.md','chunks':json.dumps([
        {'locator':f'段落 {i}','text':chapters[j:j+1000]}
        for i,j in enumerate(range(0,len(chapters),1000),1)
    ],ensure_ascii=False)}
    read=[]
    def fake_model(system,user):
        if '长文档的一部分' in system:
            read.append(user)
            return {'summary': f'该批包含：{user[-80:]}', 'scope':'对象及结构', 'questions':[]}
        return {'summary':'长文档包含前后全部业务章节','scope':'设备、属性、关系及质量规范','questions':[]}
    with brain.using_model({'base_url':'https://stub/v1','model':'stub','api_key':''}):
        monkeypatch.setattr(brain,'query_llm',fake_model)
        result=brain.analyze([doc])
    assert len(read)>1
    sent=''.join(read)
    assert '第1章' in sent and '第11章' in sent
    # Parser coverage belongs to machine-readable metadata, not editable overview.
    assert '【解析覆盖】' not in result['summary']
    assert result['coverage']['complete'] is True
    assert result['coverage']['batches'] == len(read)
    assert any('第11章' in x for x in result['coverage']['chapter_headings'])


def test_existing_uploads_can_be_reparsed_from_original_files(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENV','development')
    for key in ('LLM_MODEL','LLM_BASE_URL','API_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    doc=Document()
    doc.add_paragraph('第6章 实体属性定义')
    doc.add_paragraph('项目字段'+('属性的长篇定义。'*1500)+'第11章修订结束')
    stream=io.BytesIO();doc.save(stream)
    with TestClient(create_app(tmp_path/'appdata')) as c:
        pid=c.post('/api/projects',json={'name':'修复旧截断'}).json()['project']['id']
        uploaded=c.post(f'/api/projects/{pid}/documents',files={'file':('spec.docx',stream.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        assert uploaded.status_code==200, uploaded.text
        docid=uploaded.json()['document']['id']
        db=tmp_path/'appdata'/'ontology.sqlite3'
        with sqlite3.connect(db) as con:
            con.execute('UPDATE documents SET content=?,chunks=? WHERE id=?',('已截断',json.dumps([{'locator':'段落 1','text':'已截断'}]),docid))
        resp=c.post(f'/api/projects/{pid}/documents/reparse')
        assert resp.status_code==200,resp.text
        r=resp.json()
        assert r['reparsed']==1 and r['documents'][0]['after_chars']>r['documents'][0]['before_chars']
        raw=c.get(f'/api/projects/{pid}/documents/{docid}/chunks').json()['chunks']
        alltext=''.join(ch['text'] for ch in raw)
        assert '第6章' in alltext and '第11章修订结束' in alltext
        assert c.get(f'/api/projects/{pid}').json()['project']['scope_confirmed']==0


def test_xlsx_csv_and_long_word_paragraph_have_no_silent_field_truncation(tmp_path):
    word=Document();word.add_paragraph('章节'+ '长文本'*2100 + '字段结束')
    wp=tmp_path/'long.docx';word.save(wp)
    data=extract(wp)
    assert '字段结束' in data['content']
    assert any('片段' in x['locator'] for x in data['chunks'])
    book=Workbook();book.active['A1']='表格开始'+'结构化属性'*1000+'表格结束'
    path=tmp_path/'big.xlsx';book.save(path)
    assert '表格结束' in extract(path)['content']
    csv=tmp_path/'rows.csv';csv.write_text('name,description\nlong,'+'关系解释'*1000+'CSV结束',encoding='utf-8')
    assert 'CSV结束' in extract(csv)['content']


@pytest.mark.parametrize('raw,expected',[
    ('<think>我先推理</think>```json\n{"proposals": []}\n```','proposals'),
    ('结果如下：\n{"proposals": [{"kind":"entity","payload":{"name":"设备"}}]}。','proposals'),
    ('前言 {无效} 最终结论：{"proposals":[]}','proposals'),
])
def test_json_parser_accepts_complete_objects_surrounded_by_text(raw,expected):
    assert expected in brain._extract_json(raw)


class _Response:
    def __init__(self,content,finish_reason='stop'):
        self.content=content;self.finish_reason=finish_reason
    def raise_for_status(self):pass
    def json(self):return {'choices':[{'message':{'content':self.content},'finish_reason':self.finish_reason}]}


def test_entity_generation_retries_truncated_json_and_succeeds(monkeypatch):
    calls=[]
    responses=[_Response('{"proposals":[{"kind":"entity"', 'length'),
               _Response('```json\n{"proposals":[{"kind":"entity","payload":{"name":"设备"},"evidence":[]}]}\n```')]
    class FakeClient:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            calls.append(kwargs['json'])
            return responses.pop(0)
    monkeypatch.setattr(brain,'_pooled_http_client',lambda:FakeClient())
    with brain.using_model({'base_url':'http://fake/v1','model':'mock','api_key':''}):
        result=brain.query_llm('请给出实体候选','实体：设备')
    assert len(calls)==2
    assert calls[0]['max_tokens']>=4096
    assert result['proposals'][0]['payload']['name']=='设备'
    assert '合法 JSON' in calls[1]['messages'][0]['content']


def test_invalid_json_fails_after_bounded_retries_with_actionable_error(monkeypatch):
    attempts=[]
    class FakeClient:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            attempts.append(1)
            return _Response('{"proposals": [')
    monkeypatch.setattr(brain,'_pooled_http_client',lambda:FakeClient())
    with brain.using_model({'base_url':'http://fake/v1','model':'mock','api_key':''}):
        with pytest.raises(brain.ModelFormatError,match='连续 3 次'):
            brain.query_llm('实体候选','实体：设备')
    assert len(attempts)==3


def test_invalid_proposals_schema_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *_: {'result': []})
    d={'id':'d','filename':'test.txt','chunks':json.dumps([{'locator':'行1','text':'实体：设备'}])}
    with brain.using_model({'base_url':'http://fake/v1','model':'mock','api_key':''}):
        with pytest.raises(brain.ModelFormatError,match='proposals'):
            brain.propose('ENTITY',[d],{'entities':[],'attributes':[],'relations':[]},'设备')
