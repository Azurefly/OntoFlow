"""OpenMind connection must be managed from UI/API without exposing credentials."""
import json
import os

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import create_app
from app.openmind import OpenMindClient
from app.openmind_settings import OpenMindSettings
from app.store import Store


def configured_env(monkeypatch):
    monkeypatch.setenv('APP_ENV','development')
    for key in ('API_TOKEN','OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN','OPENMIND_ALLOWED_HOSTS',
                'OPENMIND_ADMIN_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY'):
        monkeypatch.delenv(key,raising=False)


def test_ui_config_persists_without_plaintext_and_no_secret_echo(tmp_path,monkeypatch):
    configured_env(monkeypatch)
    data=tmp_path/'data'
    with TestClient(create_app(data)) as client:
        r=client.put('/api/settings/openmind',json={
            'base_url':'https://openmind.example','allowed_hosts':['openmind.example'],
            'access_token':'bearer-demo-not-for-browser','new_admin_token':'new-secret-12-chars'})
        assert r.status_code==200,r.text
        public=r.json()
        assert public['configured'] and public['source']=='saved'
        assert public['has_access_token'] and public['has_admin_token']
        assert public['access_token'] is None and public['admin_token'] is None
        assert 'bearer-demo-not-for-browser' not in json.dumps(public)
        assert 'new-secret-12-chars' not in json.dumps(public)
        encrypted=(data/'ontology.sqlite3').read_bytes()
        assert b'bearer-demo-not-for-browser' not in encrypted
        assert b'new-secret-12-chars' not in encrypted
        assert client.put('/api/settings/openmind',json={
            'base_url':'https://openmind.example','allowed_hosts':['openmind.example']}).status_code==403
    with TestClient(create_app(data)) as reloaded:
        public=reloaded.get('/api/settings/openmind').json()
        assert public['has_access_token']
        assert reloaded.put('/api/settings/openmind',headers={'X-OpenMind-Admin-Token':'new-secret-12-chars'},json={
            'base_url':'https://openmind.example','allowed_hosts':['openmind.example']}).status_code==200
        assert reloaded.app.state.openmind_settings.effective()['access_token']=='bearer-demo-not-for-browser'
        assert reloaded.delete('/api/settings/openmind',headers={'X-OpenMind-Admin-Token':'new-secret-12-chars'}).status_code==200
        assert reloaded.get('/api/settings/openmind').json()['source']=='environment'


def test_endpoint_change_clears_old_remote_token_and_rejects_bad_hosts(tmp_path,monkeypatch):
    configured_env(monkeypatch)
    with TestClient(create_app(tmp_path/'state')) as client:
        for url,hosts in [('file:///etc/passwd',['openmind.example']),
                          ('https://openmind.example',['other.example']),
                          ('https://openmind.example/path',['openmind.example']),
                          ('https://openmind.example',['https://openmind.example'])]:
            assert client.put('/api/settings/openmind',json={'base_url':url,'allowed_hosts':hosts}).status_code==422
        ok=client.put('/api/settings/openmind',json={'base_url':'https://one.example',
            'allowed_hosts':['one.example'],'access_token':'must-not-migrate'})
        assert ok.status_code==200,ok.text
        changed=client.put('/api/settings/openmind',json={'base_url':'https://two.example',
            'allowed_hosts':['two.example']})
        assert changed.status_code==200
        assert not changed.json()['has_access_token']


def test_production_admin_bootstrap_and_tls(tmp_path,monkeypatch):
    configured_env(monkeypatch)
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('API_TOKEN','app-token')
    monkeypatch.setenv('MODEL_ADMIN_TOKEN','root-admin')
    monkeypatch.setenv('MODEL_SETTINGS_KEY',Fernet.generate_key().decode())
    with TestClient(create_app(tmp_path/'prod',testing=True)) as client:
        user={'X-API-Key':'app-token'}
        admin={**user,'X-Model-Admin-Key':'root-admin'}
        body={'base_url':'https://openmind.internal','allowed_hosts':['openmind.internal'],
              'access_token':'real-user-token','new_admin_token':'om-admin-secret-1234'}
        assert client.put('/api/settings/openmind',headers=user,json=body).status_code==403
        assert client.put('/api/settings/openmind',headers=admin,json={**body,'base_url':'http://openmind.internal'}).status_code==422
        r=client.put('/api/settings/openmind',headers=admin,json=body)
        assert r.status_code==200,r.text
        new_admin={**user,'X-OpenMind-Admin-Token':'om-admin-secret-1234'}
        assert client.put('/api/settings/openmind',headers=new_admin,json={
            'base_url':'https://openmind.internal','allowed_hosts':['openmind.internal']}).status_code==200
        assert client.get('/api/settings/openmind',headers=user).json()['has_access_token']
        assert client.get('/api/settings/openmind').status_code==401


def test_global_connection_test_uses_saved_credentials(tmp_path,monkeypatch):
    configured_env(monkeypatch)
    calls=[]
    original=httpx.Client
    def mock_client(**kwargs):
        def handle(request):
            calls.append((str(request.url),request.headers.get('authorization')))
            return httpx.Response(200,json={'items':[],'total':0})
        kwargs['transport']=httpx.MockTransport(handle)
        return original(**kwargs)
    monkeypatch.setattr(httpx,'Client',mock_client)
    with TestClient(create_app(tmp_path/'state')) as client:
        saved=client.put('/api/settings/openmind',json={
            'base_url':'https://target.example','allowed_hosts':['target.example'],
            'access_token':'target-user-secret'})
        assert saved.status_code==200,saved.text
        status=client.post('/api/settings/openmind/test')
        assert status.status_code==200,status.text
        assert status.json()['read_only']
        assert calls==[('https://target.example/api/v1/domains','Bearer target-user-secret')]


def test_legacy_env_config_is_still_supported(tmp_path,monkeypatch):
    configured_env(monkeypatch)
    monkeypatch.setenv('OPENMIND_BASE_URL','https://legacy.example')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','legacy-token')
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS','legacy.example')
    with TestClient(create_app(tmp_path/'state')) as client:
        state=client.get('/api/settings/openmind').json()
        assert state['source']=='environment' and state['configured']
        assert 'legacy-token' not in json.dumps(state)
        assert client.app.state.openmind_settings.effective()['access_token']=='legacy-token'
