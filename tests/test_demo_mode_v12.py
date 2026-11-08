"""Demo mode must avoid OntoFlow admin secrets but preserve remote auth requirements."""
import json

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app


def demo_env(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_DEMO_MODE', 'true')
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS', 'models.internal')
    monkeypatch.setenv('API_TOKEN', 'unused-api-secret')
    monkeypatch.setenv('MODEL_ADMIN_TOKEN', 'unused-model-admin-secret')
    monkeypatch.delenv('MODEL_SETTINGS_KEY', raising=False)


def test_demo_disables_only_internal_keys_and_works_without_fernet(tmp_path, monkeypatch):
    demo_env(monkeypatch)
    with TestClient(create_app(tmp_path/'demo',testing=True)) as client:
        assert client.get('/api/health').json()['demo_mode'] is True
        assert client.get('/api/settings/model').status_code == 200
        result = client.put('/api/settings/model', json={
            'base_url':'http://models.internal/v1', 'model':'demo-model', 'api_key':'provider-secret'})
        assert result.status_code == 200, result.text
        assert result.json()['has_api_key']
        assert result.json()['api_key'] is None
        assert client.app.state.model_settings.effective()['api_key']=='provider-secret'
        assert client.get('/api/projects').status_code==200
        saved=client.put('/api/settings/openmind',json={
            'base_url':'https://om.internal','allowed_hosts':['om.internal'],
            'auth_mode':'login','username':'demo-agent','password':'openmind-account-password'})
        assert saved.status_code==200,saved.text
        assert saved.json()['configured'] is True
        assert saved.json()['has_admin_token'] is False
        assert 'openmind-account-password' not in saved.text
        assert client.app.state.openmind_settings.effective()['password']=='openmind-account-password'
        # Both secrets are reversible in local DB and never sent in GET responses.
        stored=client.app.state.store.one('SELECT api_key_ciphertext FROM model_settings WHERE id=1')
        assert stored['api_key_ciphertext']=='demo-plain-v1:provider-secret'
        stored_om=client.app.state.store.one('SELECT password_ciphertext FROM openmind_settings WHERE id=1')
        assert stored_om['password_ciphertext']=='demo-plain-v1:openmind-account-password'
        assert client.delete('/api/settings/openmind').status_code==200
        assert client.delete('/api/settings/model').status_code==200
        assert client.put('/api/settings/openmind',json={
            'base_url':'https://om.internal','allowed_hosts':['om.internal'],
            'auth_mode':'login','username':'demo-agent','password':''}).status_code==422
        # No removal of remote OpenMind password rules.
        bad=client.put('/api/settings/model',json={'base_url':'file:///etc/passwd','model':'nope'})
        assert bad.status_code==422


def test_demo_can_repair_old_encrypted_values_with_lost_master_key(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('ONTOFLOW_DEMO_MODE',raising=False)
    original=Fernet.generate_key().decode()
    monkeypatch.setenv('MODEL_SETTINGS_KEY',original)
    state=tmp_path/'existing'
    with TestClient(create_app(state)) as secure:
        conf=secure.put('/api/settings/model',json={
            'base_url':'https://models.internal/v1','model':'legacy',
            'api_key':'original-provider-secret'})
        assert conf.status_code==200,conf.text
        om=secure.put('/api/settings/openmind',json={
            'base_url':'https://om.internal','allowed_hosts':['om.internal'],
            'auth_mode':'login','username':'old-account','password':'original-password',
            'new_admin_token':'old-admin-key-12345'})
        assert om.status_code==200,om.text
    demo_env(monkeypatch)
    with TestClient(create_app(state,testing=True)) as demo:
        model=demo.get('/api/settings/model')
        assert model.status_code==200,model.text
        assert model.json()['credential_reset_required'] is True
        assert not model.json()['has_api_key']
        om=demo.get('/api/settings/openmind')
        assert om.status_code==200,om.text
        assert om.json()['credential_reset_required'] is True
        assert not om.json()['has_password']
        result=demo.put('/api/settings/model',json={
            'base_url':'https://models.internal/v1','model':'legacy',
            'api_key':'new-provider-secret'})
        assert result.status_code==200,result.text
        result=demo.put('/api/settings/openmind',json={
            'base_url':'https://om.internal','allowed_hosts':['om.internal'],
            'auth_mode':'login','username':'old-account','password':'new-openmind-password'})
        assert result.status_code==200,result.text
        assert demo.app.state.model_settings.effective()['api_key']=='new-provider-secret'
        assert demo.app.state.openmind_settings.effective()['password']=='new-openmind-password'
        assert demo.get('/api/settings/model').json()['credential_reset_required'] is False
        assert demo.get('/api/settings/openmind').json()['credential_reset_required'] is False
    # In secure mode, Demo plaintext is rejected instead of silently accepted.
    monkeypatch.setenv('ONTOFLOW_DEMO_MODE','false')
    monkeypatch.setenv('MODEL_SETTINGS_KEY',original)
    with TestClient(create_app(state,testing=True)) as secure_again:
        response=secure_again.get('/api/settings/model')
        assert response.status_code>=400
