"""Model settings must be integrated, durable, confidential, and actually used by workflow."""
import json
import os
import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from app import brain
from app.main import create_app


def assert_ok(result, code=200):
    assert result.status_code == code, result.text
    return result.json()


def test_page_settings_persist_and_drive_existing_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    for key in ('API_TOKEN','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','MODEL_SETTINGS_KEY','MODEL_ALLOWED_HOSTS','MODEL_ADMIN_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    data=tmp_path/'data'
    with TestClient(create_app(data)) as client:
        before=assert_ok(client.get('/api/settings/model'))
        assert before['mode']=='RULE_DEMO'
        assert before['api_key'] is None
        conf=assert_ok(client.put('/api/settings/model', json={
            'base_url':'https://models.example.test/v1/', 'model':'some-instruct-model', 'api_key':'secret-never-returned'}))
        assert conf['mode']=='LLM' and conf['source']=='saved' and conf['has_api_key']
        assert conf['base_url']=='https://models.example.test/v1'
        assert 'secret-never-returned' not in str(conf)
        assert 'secret-never-returned' not in (data/'ontology.sqlite3').read_bytes().decode('latin1')
        assert 'secret-never-returned' not in (data/'.model-settings.key').read_text()
        assert os.stat(data/'.model-settings.key').st_mode & 0o077 == 0
        seen=[]
        def fake_model(system, user):
            active=brain.active_model()
            seen.append((active['base_url'],active['model'],active['api_key']))
            if '内容概要' in system:
                return {'summary':'AI识别了网关与主板','scope':'网关设备结构','questions':[]}
            if '本体工程师' in system:
                return {'proposals':[{'kind':'entity','payload':{'name':'主板','kind':'class','description':'网关组件'},'evidence':[]}]}
            return {'reply':'建议保留主板类。','proposals':[]}
        monkeypatch.setattr(brain, 'query_llm', fake_model)
        pid=assert_ok(client.post('/api/projects',json={'name':'UI配置联动'}))['project']['id']
        assert assert_ok(client.get(f'/api/projects/{pid}'))['engine']=={'mode':'LLM','model':'some-instruct-model'}
        assert_ok(client.post(f'/api/projects/{pid}/documents',files={'file':('gateway.txt','包含：主板'.encode(),'text/plain')}))
        summary=assert_ok(client.post(f'/api/projects/{pid}/analyze'))
        assert summary['summary']=='AI识别了网关与主板'
        assert_ok(client.put(f'/api/projects/{pid}/topic',json={'summary':summary['summary'],'scope':summary['scope']}))
        proposal=assert_ok(client.post(f'/api/projects/{pid}/propose'))
        assert len(proposal['created'])==1
        reply=assert_ok(client.post(f'/api/projects/{pid}/chat',json={'message':'主板需要吗？'}))
        assert '主板' in reply['reply']
        assert len(seen)==3
        assert all(item==('https://models.example.test/v1','some-instruct-model','secret-never-returned') for item in seen)
        monkeypatch.setattr(brain, 'test_connection', lambda cfg: {'ok':True,'model':cfg['model'],'reply_preview':'连接成功'})
        assert assert_ok(client.post('/api/settings/model/test'))['ok']
    with TestClient(create_app(data)) as client:
        # Docker restart/new app must reload backend DB config.
        status=assert_ok(client.get('/api/settings/model'))
        assert status['mode']=='LLM' and status['has_api_key'] and status['api_key'] is None
        assert_ok(client.delete('/api/settings/model'))
        assert assert_ok(client.get('/api/settings/model'))['source']=='environment'
        assert assert_ok(client.get('/api/projects/'+pid))['engine']['mode']=='RULE_DEMO'


def test_settings_validation_clear_key_and_admin_access(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('API_TOKEN','only-project-api-token')
    monkeypatch.setenv('MODEL_ADMIN_TOKEN','admin-only-token')
    monkeypatch.setenv('MODEL_SETTINGS_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS','inference.internal,models.internal')
    for k in ('LLM_BASE_URL','LLM_MODEL','LLM_API_KEY'):
        monkeypatch.delenv(k,raising=False)
    with TestClient(create_app(tmp_path/'prod',testing=True)) as client:
        token={'X-API-Key':'only-project-api-token'}
        admin={**token, 'X-Model-Admin-Key':'admin-only-token'}
        assert client.put('/api/settings/model',json={'base_url':'http://inference.internal/v1','model':'m'}).status_code==401
        assert client.put('/api/settings/model',headers=token,json={'base_url':'http://inference.internal/v1','model':'m'}).status_code==403
        assert client.put('/api/settings/model',headers=admin,json={'base_url':'file:///etc/shadow','model':'m'}).status_code==422
        assert client.put('/api/settings/model',headers=admin,json={'base_url':'http://untrusted.example/v1','model':'m'}).status_code==422
        saved=assert_ok(client.put('/api/settings/model',headers=admin,json={'base_url':'http://inference.internal/v1','model':'m','api_key':'first-secret'}))
        assert saved['has_api_key']
        changed=assert_ok(client.put('/api/settings/model',headers=admin,json={'base_url':'http://models.internal/v1','model':'m2'}))
        assert not changed['has_api_key']  # no accidental forwarding of prior provider key
        assert assert_ok(client.get('/api/settings/model',headers=token))['api_key'] is None
        assert client.post('/api/settings/model/test',headers=token).status_code==403
        assert_ok(client.delete('/api/settings/model',headers=admin))


def test_real_http_contract_uses_model_secret_only_as_bearer(monkeypatch):
    captured={}
    def handle(request):
        captured['url']=str(request.url)
        captured['authorization']=request.headers.get('authorization')
        captured['body']=json.loads(request.content)
        return httpx.Response(200,json={'model':'demo','choices':[{'message':{'content':'连接成功'}}]})
    original=httpx.Client
    monkeypatch.setattr(brain.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    config={'base_url':'https://model.example/v1','model':'demo','api_key':'a-private-key'}
    assert brain.test_connection(config)['reply_preview']=='连接成功'
    assert captured['url']=='https://model.example/v1/chat/completions'
    assert captured['authorization']=='Bearer a-private-key'
    assert captured['body']['model']=='demo'
    assert 'a-private-key' not in json.dumps(captured['body'])
