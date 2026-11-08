"""Demo connection policy regressions: no deployments/credentials touched.

Exercise settings save/read, outgoing clients, HTTP login/refresh and strict-mode
control cases. All external endpoints use MockTransport or are syntax-only.
"""
from __future__ import annotations

import importlib
import json
import socket
import time
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.model_settings import ModelSettingsError, validate_endpoint
from app.openmind import OpenMindClient, OpenMindError, validate_base_url, uuid_value
from app.openmind_auth import OpenMindAutoClient
from app.openmind_settings import OpenMindSettings, validate_connection
from app.openmind_transport_policy import insecure_http_enabled
from app.store import Store
from app.version import VERSION, SCHEMA_VERSION
from scripts.init_fresh_env import generate_settings


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_DEMO_MODE', 'true')
    monkeypatch.setenv('APP_ENV', 'production')
    # Even old/mismatched deployment settings must be ignored in Demo.
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS', 'retired-model.example')
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS', 'https://obsolete.example:8443/*')
    monkeypatch.setenv('OPENMIND_ALLOW_INSECURE_HTTP', 'false')
    monkeypatch.setenv('API_TOKEN', 'unused-internal-token')
    monkeypatch.setenv('MODEL_ADMIN_TOKEN', 'unused-admin-token')
    monkeypatch.delenv('MODEL_SETTINGS_KEY', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)


@pytest.mark.parametrize('url', [
    'http://192.168.20.30:8080', 'http://host.docker.internal:8081',
    'http://openmind.internal:80', 'http://100.80.9.10:8090',
    'http://203.0.113.25:8000', 'https://unlisted.example',
    'http://localhost:9999', 'http://[::1]:8888',
])
def test_demo_both_url_gates_ignore_allowlists_without_dns_precheck(monkeypatch,url):
    def forbidden(*args,**kwargs):
        raise AssertionError('Policy must not resolve or filter DNS in Demo')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    assert validate_connection(url,[]) == url
    assert validate_connection(url,['wrong.example']) == url
    assert validate_base_url(url,[]) == url
    assert validate_base_url(url,['https://bad-whitelist-value/*']) == url
    assert insecure_http_enabled() is True


@pytest.mark.parametrize('url', ['http://host.docker.internal:11434/v1',
                                'https://unlisted-model.example/v1'])
def test_demo_model_endpoint_ignores_deployment_allowlist(url):
    assert validate_endpoint(url)==url


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://user:secret@host',
                                'http://host:99999','http://[broken',
                                'https://host?token=do-not-use-query',
                                'https://host#secret'])
def test_real_url_format_remains_validated(url):
    with pytest.raises(OpenMindError):validate_connection(url,[])
    with pytest.raises(OpenMindError):validate_base_url(url,[])
    with pytest.raises(ModelSettingsError):validate_endpoint(url)


def test_openmind_root_and_model_api_base_are_distinct():
    with pytest.raises(OpenMindError):validate_connection('http://host:8080/api/v1',[])
    assert validate_endpoint('http://host:8080/v1')=='http://host:8080/v1'


def test_new_demo_can_save_model_and_openmind_without_any_admin_or_hosts(tmp_path):
    with TestClient(create_app(tmp_path/'data',testing=True)) as client:
        saved=client.put('/api/settings/openmind',json={
            'base_url':'http://host.docker.internal:8080','auth_mode':'login',
            'username':'integration','password':'example-secret'})
        assert saved.status_code==200,saved.text
        config=saved.json()
        assert config['configured'] and config['demo_mode']
        assert config['allowed_hosts']==[] and config['http_compat_enabled']
        assert config['connection_policy']=='demo_unrestricted'
        assert 'example-secret' not in saved.text
        model=client.put('/api/settings/model',json={
            'base_url':'http://host.docker.internal:11434/v1','model':'local-model',
            'api_key':'example-provider-secret'})
        assert model.status_code==200,model.text
        assert model.json()['connection_policy']=='demo_unrestricted'
        assert 'example-provider-secret' not in model.text
        health=client.get('/api/health').json()
        assert health['connection_policy']=='demo_unrestricted'
        assert health['version']==client.app.version==VERSION
        assert SCHEMA_VERSION=='1.4.0'  # additive intent/decision/provenance tables
        assert client.app.state.store.one("SELECT name FROM sqlite_master WHERE name='relation_labels'")


def test_old_browser_still_sending_invalid_hosts_does_not_break_demo(tmp_path):
    with TestClient(create_app(tmp_path/'data',testing=True)) as client:
        r=client.put('/api/settings/openmind',json={
            'base_url':'http://new-target.internal:8888','access_token':'example-token',
            'allowed_hosts':['https://obsolete.example/*','wrong-host:1234','*']})
        assert r.status_code==200,r.text
        assert r.json()['allowed_hosts']==[]


def test_existing_connection_reloads_ignoring_stale_hosts_without_erasing_them(tmp_path):
    store=Store(tmp_path/'db');settings=OpenMindSettings(store)
    settings.save('http://existing.internal:8080',auth_mode='login',
                  username='saved-user',password='saved-password')
    old_hosts='["previous.internal"]'
    with store.tx() as c:c.execute('UPDATE openmind_settings SET allowed_hosts=? WHERE id=1',(old_hosts,))
    reloaded=OpenMindSettings(Store(tmp_path/'db'))
    assert reloaded.effective()['password']=='saved-password'
    assert reloaded.effective()['allowed_hosts']==[]
    assert reloaded.public()['configured']
    reloaded.save('http://existing.internal:8080',auth_mode='login',username='saved-user')
    assert store.one('SELECT allowed_hosts FROM openmind_settings WHERE id=1')['allowed_hosts']==old_hosts
    assert reloaded.effective()['password']=='saved-password'
    # A dormant malformed list must not even be decoded on Demo reads/saves.
    with store.tx() as c:c.execute('UPDATE openmind_settings SET allowed_hosts=? WHERE id=1',('malformed-old-list',))
    assert reloaded.public()['configured']
    assert reloaded.save('http://existing.internal:8080')['configured']


def test_environment_only_connection_needs_no_whitelist(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENMIND_BASE_URL','http://host.docker.internal:8080')
    monkeypatch.setenv('OPENMIND_AUTH_MODE','login')
    monkeypatch.setenv('OPENMIND_USERNAME','env-user')
    monkeypatch.setenv('OPENMIND_PASSWORD','env-password')
    settings=OpenMindSettings(Store(tmp_path/'data'))
    assert settings.public()['configured']
    assert settings.effective()['allowed_hosts']==[]
    assert settings.effective()['password']=='env-password'


def test_host_change_still_does_not_forward_old_credentials(tmp_path):
    store=Store(tmp_path/'state');settings=OpenMindSettings(store)
    settings.save('http://one.internal:8080',access_token='example-old-token')
    changed=settings.save('http://two.internal:8080')
    assert not changed['has_access_token']
    settings.save('http://one.internal:8080',auth_mode='login',username='user',password='example-pwd')
    with pytest.raises(OpenMindError,match='密码'):
        settings.save('http://two.internal:8080',auth_mode='login',username='user')


def _login_response(token):
    return httpx.Response(200,json={'sessionType':'FULL','accessToken':token,
        'expiresAt':int(time.time())+3600,'idleTimeoutSeconds':10800,
        'user':{'username':'integration','displayName':'test-account'}},headers=[
        ('Set-Cookie',f'om_refresh=refresh-{token}; Path=/api/v1/auth; HttpOnly'),
        ('Set-Cookie',f'om_csrf=csrf-{token}; Path=/')])


def test_http_auto_login_refresh_and_api_work_without_whitelists():
    calls=[]
    def handle(request):
        route=request.url.path;calls.append(route)
        if route.endswith('/login'):
            assert json.loads(request.content)=={'username':'integration','password':'example-password'}
            return _login_response('one')
        if route.endswith('/me'):
            assert request.headers['authorization']=='Bearer one'
            return httpx.Response(200,json={'username':'integration'})
        if route.endswith('/refresh'):
            assert request.headers['x-csrf-token']=='csrf-one'
            assert 'om_refresh=refresh-one' in request.headers['cookie']
            return _login_response('two')
        assert request.headers['authorization'] in ('Bearer one','Bearer two')
        return httpx.Response(200,json={'items':[],'total':0})
    remote=OpenMindAutoClient('http://host.docker.internal:8080','integration','example-password',
                            allowed_hosts=['wrong.example'],transport=httpx.MockTransport(handle))
    try:
        assert remote.domains()['items']==[]
        remote._expires_at=time.time()+1
        assert remote.domains()['items']==[]
        assert remote.token=='two'
        assert calls.count('/api/v1/auth/login')==1
        assert calls.count('/api/v1/auth/refresh')==1
        assert not remote.client.follow_redirects
    finally:remote.close()


def test_saved_config_test_endpoint_uses_actual_auto_client(tmp_path,monkeypatch):
    module=importlib.import_module('app.main')
    requests=[]
    def handle(req):
        requests.append(req.url.path)
        if req.url.path.endswith('/login'):return _login_response('token')
        if req.url.path.endswith('/me'):return httpx.Response(200,json={'username':'integration'})
        assert req.headers['authorization']=='Bearer token'
        return httpx.Response(200,json={'items':[],'total':0})
    original=OpenMindAutoClient
    monkeypatch.setattr(module,'OpenMindAutoClient',lambda *a,**kw:
                        original(*a,transport=httpx.MockTransport(handle),**kw))
    with TestClient(create_app(tmp_path/'data',testing=True)) as client:
        r=client.put('/api/settings/openmind',json={
            'base_url':'http://host.docker.internal:8080','auth_mode':'login',
            'username':'integration','password':'example-password'})
        assert r.status_code==200,r.text
        tested=client.post('/api/settings/openmind/test')
        assert tested.status_code==200,tested.text
        assert tested.json()['connected'] and tested.json()['read_only']
        domains=client.get('/api/settings/openmind/domains')
        assert domains.status_code==200,domains.text
        assert requests.count('/api/v1/auth/login')==1
        assert 'example-password' not in tested.text


def test_model_test_endpoint_uses_config_without_whitelist(tmp_path,monkeypatch):
    from app import brain
    calls=[];original=httpx.Client
    def handle(req):
        calls.append(str(req.url))
        assert req.headers['authorization']=='Bearer example-key'
        return httpx.Response(200,json={'choices':[{'message':{'content':'connection ok'}}]})
    monkeypatch.setattr(brain.httpx,'Client',lambda **kw:
                        original(transport=httpx.MockTransport(handle),**kw))
    with TestClient(create_app(tmp_path/'data',testing=True)) as client:
        saved=client.put('/api/settings/model',json={
            'base_url':'http://host.docker.internal:11434/v1','model':'demo','api_key':'example-key'})
        assert saved.status_code==200,saved.text
        tested=client.post('/api/settings/model/test')
        assert tested.status_code==200,tested.text
        assert tested.json()['ok']
    assert calls==['http://host.docker.internal:11434/v1/chat/completions']


def test_remote_rejection_remains_remote_rejection():
    remote=OpenMindAutoClient('http://openmind.internal:8080','integration','wrong-password',
        transport=httpx.MockTransport(lambda _:httpx.Response(401,json={'code':'INVALID_CREDENTIALS'})))
    try:
        with pytest.raises(OpenMindError,match='401'):remote.domains()
    finally:remote.close()
    with pytest.raises(OpenMindError):uuid_value('not-a-uuid','对象')


def test_standard_mode_still_enforces_configured_policies(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_DEMO_MODE','false')
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS','allowed.example')
    with pytest.raises(OpenMindError,match='页面白名单'):validate_connection('https://allowed.example',[])
    with pytest.raises(OpenMindError,match='白名单'):validate_base_url('https://other.example',['allowed.example'])
    with pytest.raises(OpenMindError,match='HTTPS'):validate_connection('http://allowed.example',['allowed.example'])
    with pytest.raises(ModelSettingsError,match='白名单'):validate_endpoint('https://new-model.example/v1')
    assert validate_connection('https://allowed.example',['allowed.example'])=='https://allowed.example'


def test_new_env_does_not_require_obsolete_host_flags():
    lines=generate_settings().splitlines()
    actual={k:v for k,v in (line.split('=',1) for line in lines if line and not line.startswith('#'))}
    assert actual['ONTOFLOW_DEMO_MODE']=='true'
    assert all(k not in actual for k in ('MODEL_ALLOWED_HOSTS','OPENMIND_ALLOWED_HOSTS','OPENMIND_ALLOW_INSECURE_HTTP'))
    assert actual['PG_PASSWORD']


def test_demo_frontend_renders_without_whitelist_and_saves():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable: frontend execution requires Node')
    script=Path(__file__).with_name('demo_connections_v138.cjs')
    result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=12,check=False)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
