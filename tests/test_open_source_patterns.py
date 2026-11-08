"""Regression tests based on public Docling/LlamaIndex/Instructor patterns."""
import json
import re
from fastapi.testclient import TestClient
import pytest
from app import brain
from app.main import create_app


def test_multiple_json_documents_are_not_silently_misread():
    with pytest.raises(brain.ModelFormatError, match='多个完整 JSON'):
        brain._extract_json('示例：{"proposals":[]} 实际：{"proposals":[{"kind":"entity"}]}')


def test_schema_validation_reasks_semantically_invalid_json(monkeypatch):
    calls = []
    outputs = [
        {'proposals': [{'kind': 'entity', 'payload': {'description': '缺少名称'}, 'evidence': []}]},
        {'proposals': [{'kind': 'entity', 'payload': {'name': '网关', 'kind': 'class'}, 'evidence': []}]},
    ]
    def stub(system, user):
        calls.append(system)
        return outputs.pop(0)
    monkeypatch.setattr(brain, 'query_llm', stub)
    with brain.using_model({'base_url': 'http://stub/v1', 'model': 'fake', 'api_key': ''}):
        r = brain.proposal_batches('ENTITY', [
            {'id': 'd1', 'filename': 't.txt', 'chunks': json.dumps([{'locator': 'L1', 'text': '网关'}])}
        ], {'entities': [], 'attributes': [], 'relations': []}, '网关')
    assert len(calls) == 2
    assert '上次响应未通过结构验证' in calls[1]
    assert r['items'][0]['payload']['name'] == '网关'


def test_malformed_schema_has_clear_error_no_silent_fallback(monkeypatch):
    monkeypatch.setattr(brain, 'query_llm', lambda *_: {'proposals': '不是数组'})
    with brain.using_model({'base_url': 'http://stub/v1', 'model': 'fake', 'api_key': ''}):
        with pytest.raises(brain.ModelFormatError, match='本体候选结构契约'):
            brain.proposal_batches('ENTITY', [
                {'id': 'd1', 'filename': 't.txt', 'chunks': json.dumps([{'locator':'L1','text':'实体：网关'}])}
            ], {'entities': [], 'attributes': [], 'relations': []}, '网关')


def test_entity_batches_resume_and_cover_final_chapter(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    for k in ('LLM_BASE_URL', 'LLM_MODEL', 'API_TOKEN'):
        monkeypatch.delenv(k, raising=False)
    per_batch = []
    def stub_model(system, user):
        if '谨慎的本体工程师' in system:
            info = json.loads(user)
            per_batch.append((info['batch'], info['document_text']))
            return {'proposals': [{'kind': 'entity', 'payload': {
                'name': f'第{info["batch"]}批对象', 'kind': 'class', 'description': ''
            }, 'evidence': []}]}
        return {'summary': '全文概要', 'scope': '完整本体', 'questions': []}
    monkeypatch.setattr(brain, 'query_llm', stub_model)
    # Repeated chapters exceed the per-call proposal budget and therefore must
    # be processed through multiple invocations, never by prefix-only sampling.
    text = ''.join(f'第{i}章 内容\n'+('实体属性关系描述'+str(i))*550 for i in range(1, 12))
    config = {'base_url':'http://test-model/v1','model':'mock','api_key':'','source':'test','updated_at':None}
    with TestClient(create_app(tmp_path / 'data')) as c:
        pid = c.post('/api/projects',json={'name':'分批续建'}).json()['project']['id']
        assert c.post(f'/api/projects/{pid}/documents',
                      files={'file':('long.md',text.encode(),'text/markdown')}).status_code == 200
        monkeypatch.setattr(c.app.state.model_settings, 'effective', lambda: config)
        r=c.post(f'/api/projects/{pid}/analyze')
        assert r.status_code == 200, r.text
        assert c.put(f'/api/projects/{pid}/topic',json={'summary':'全部','scope':'完整本体'}).status_code == 200
        counts=[]
        for _ in range(30):
            resp=c.post(f'/api/projects/{pid}/propose')
            assert resp.status_code == 200, resp.text
            progress=resp.json()['progress']
            counts.append(progress['processed_batches'])
            if progress['complete']:
                break
        assert len(counts)>1 and counts==sorted(counts)
        assert counts[-1] == progress['total_batches']
        assert per_batch[0][0] == 1
        assert per_batch[-1][0] == progress['total_batches']
        assert '第11章' in ''.join(chunk for _, chunk in per_batch)
        assert len(per_batch) == progress['total_batches']
        assert c.post(f'/api/projects/{pid}/propose').json()['created'] == []
