"""Contract tests based on OpenMind 2026-11-03 backend auth integration document."""
import concurrent.futures
import importlib
import threading
import time
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.openmind import OpenMindError
from app.openmind_auth import OpenMindAutoClient

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'
ENTITY='20000000-0000-4000-8000-000000000001'


def response_for_auth(access, csrf='csrf-one', refresh='refresh-one', expires=None):
    return httpx.Response(200, json={
        'sessionType':'FULL','accessToken':access,
        'expiresAt':int(expires or time.time()+900),'idleTimeoutSeconds':10800,
        'user':{'id':ENTITY,'username':'service_user','displayName':'接入账号','role':'user'}},
        headers=[('set-cookie',f'om_refresh={refresh}; Path=/api/v1/auth; HttpOnly; Secure; SameSite=Lax'),
                 ('set-cookie',f'om_csrf={csrf}; Path=/; Secure; SameSite=Lax')])


def test_auto_login_me_domains_and_cookie_refresh_singleflight():
    seen={'login':0,'refresh':0,'get':0,'csrf':[]}
    lock=threading.Lock()
    def handler(req):
        if req.url.path=='/api/v1/auth/login':
            with lock:seen['login']+=1
            assert b'"username":"service_user"' in req.content
            return response_for_auth('access-one')
        if req.url.path=='/api/v1/auth/me':
            assert req.headers['Authorization']=='Bearer access-one'
            return httpx.Response(200,json={'username':'service_user'})
        if req.url.path=='/api/v1/auth/refresh':
            with lock:
                seen['refresh']+=1
                seen['csrf'].append((req.headers.get('X-CSRF-Token'),req.headers.get('cookie')))
            return response_for_auth('access-two','csrf-two','refresh-two')
        if req.url.path=='/api/v1/domains':
            with lock:seen['get']+=1
            assert req.headers['Authorization'] in ('Bearer access-one','Bearer access-two')
            return httpx.Response(200,json={'items':[], 'total':0})
        raise AssertionError(f'unexpected path {req.url.path}')
    client=OpenMindAutoClient('https://openmind.example','service_user','secret',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        assert client.domains()['items']==[]
        assert client.session_account()['username']=='service_user'
        client._expires_at=time.time()+20
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _:client.domains(), range(16)))
        assert seen['login']==1
        assert seen['refresh']==1,seen
        assert seen['get']==17
        assert seen['csrf'][0][0]=='csrf-one'
        assert 'om_refresh=refresh-one' in seen['csrf'][0][1]
        assert 'om_csrf=csrf-one' in seen['csrf'][0][1]
        assert client.token=='access-two'
        assert {c.value for c in client.client.cookies.jar if c.name=='om_refresh'}=={'refresh-two'}
    finally:client.close()


def test_identity_only_session_is_refused():
    def handler(req):
        return httpx.Response(200,json={'sessionType':'IDENTITY','identityStatus':'PENDING',
            'expiresAt':int(time.time()+1800),'idleTimeoutSeconds':1800})
    c=OpenMindAutoClient('https://openmind.example','service_user','secret',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        with pytest.raises(OpenMindError,match='FULL'):c.domains()
    finally:c.close()


def test_refresh_timeout_does_not_replay_consumed_cookie():
    state={'login':0,'refresh':0}
    def handler(req):
        path=req.url.path
        if path=='/api/v1/auth/login':
            state['login']+=1
            return response_for_auth(f'access-{state["login"]}')
        if path=='/api/v1/auth/me':
            return httpx.Response(200,json={'username':'service_user'})
        if path=='/api/v1/auth/refresh':
            state['refresh']+=1
            raise httpx.ReadTimeout('simulated uncertain rotation')
        if path=='/api/v1/domains':return httpx.Response(200,json={'items':[]})
        raise AssertionError(path)
    c=OpenMindAutoClient('https://openmind.example','service_user','secret',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        c.domains()
        c._expires_at=time.time()+5
        with pytest.raises(OpenMindError,match='刷新请求不能盲目重放'):c.domains()
        assert state['refresh']==1
        c.domains()
        assert state=={'login':2,'refresh':1}
    finally:c.close()


def test_403_does_not_relogin_or_elevate():
    state={'login':0}
    def handler(req):
        if req.url.path=='/api/v1/auth/login':
            state['login']+=1
            return response_for_auth('access-one')
        if req.url.path=='/api/v1/auth/me':return httpx.Response(200,json={'username':'service_user'})
        if req.url.path=='/api/v1/domains':return httpx.Response(403,json={'code':'DOMAIN_READ_DENIED'})
        raise AssertionError(req.url.path)
    c=OpenMindAutoClient('https://openmind.example','service_user','secret',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        with pytest.raises(OpenMindError,match='403'):c.domains()
        assert state['login']==1
    finally:c.close()


def test_create_project_validates_domain_and_branch_before_inserting(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('OPENMIND_BASE_URL','https://openmind.example')
    monkeypatch.setenv('OPENMIND_ACCESS_TOKEN','sample')
    monkeypatch.setenv('OPENMIND_ALLOWED_HOSTS','openmind.example')
    state={'bad_catalog':False}
    class Fake:
        def __init__(self,*a,**kw):pass
        def domains(self):return {'items':[{'id':DOMAIN,'name':'设备领域','code':'device','type':'local',
            'branches':[{'id':BRANCH,'version':'v_1','status':'draft','branchType':'development'},
                        {'id':'10000000-0000-4000-8000-000000000003','version':'v_2','status':'published','branchType':'release'}]}]}
        def catalog(self,branch):
            assert branch==BRANCH
            if state['bad_catalog']:raise OpenMindError('目录暂不可用')
            return {'entities':[{'id':ENTITY,'name':'网关','code':'gateway','parentId':None}],
                    'attributes':[],'relations':[],'actions':[],'capabilities':[]}
        def close(self):pass
    monkeypatch.setattr(importlib.import_module('app.main'),'OpenMindClient',Fake)
    c=TestClient(create_app(tmp_path/'db'))
    try:
        r=c.get('/api/settings/openmind/domains');assert r.status_code==200,r.text
        assert r.json()['items'][0]['branches'][0]['selectable']
        assert not r.json()['items'][0]['branches'][1]['selectable']
        assert c.post('/api/projects',json={'name':'缺少分支'}).status_code==422
        assert c.post('/api/projects',json={'name':'非法分支','domain_id':DOMAIN,
                    'branch_id':'10000000-0000-4000-8000-000000000009'}).status_code==403
        assert c.post('/api/projects',json={'name':'发布分支','domain_id':DOMAIN,
                    'branch_id':'10000000-0000-4000-8000-000000000003'}).status_code==422
        state['bad_catalog']=True
        response=c.post('/api/projects',json={'name':'目录失败','domain_id':DOMAIN,'branch_id':BRANCH})
        assert response.status_code==502,response.text
        assert c.get('/api/projects').json()==[]
        state['bad_catalog']=False
        response=c.post('/api/projects',json={'name':'网关分析','domain_id':DOMAIN,'branch_id':BRANCH})
        assert response.status_code==200,response.text
        project_id=response.json()['project']['id']
        view=c.get(f'/api/projects/{project_id}').json()
        assert view['openmind_baseline']['entity_count']==1
        assert view['openmind_bound']
        assert view['snapshot']['entities'][0]['name']=='网关'
        # Each workspace can reference the same remote UUID independently.
        second=c.post('/api/projects',json={'name':'另一个工作空间','domain_id':DOMAIN,'branch_id':BRANCH})
        assert second.status_code==200, second.text
        assert second.json()['snapshot']['entities'][0]['id']==ENTITY
        assert len(c.get('/api/projects').json())==2
    finally:c.close()


def test_login_mode_really_used_when_creating_workspace(tmp_path,monkeypatch):
    """No manual accessToken is configured; client logs in and loads selected branch."""
    monkeypatch.setenv('APP_ENV','development')
    for k in ('OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN','OPENMIND_AUTH_MODE',
              'OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_ALLOWED_HOSTS'):
        monkeypatch.delenv(k,raising=False)
    counts={'login':0,'refresh':0}
    def handler(req):
        if req.url.path=='/api/v1/auth/login':
            counts['login']+=1
            assert req.headers.get('Authorization') is None
            assert req.content and b'service_user' in req.content
            return response_for_auth('test-access')
        if req.url.path=='/api/v1/auth/me':
            assert req.headers['Authorization']=='Bearer test-access'
            return httpx.Response(200,json={'username':'service_user'})
        if req.url.path=='/api/v1/domains':
            return httpx.Response(200,json={'items':[{'id':DOMAIN,'name':'业务领域','code':'business','type':'local',
                'branches':[{'id':BRANCH,'branchType':'development','status':'draft','version':'v1'}]}]})
        if req.url.path=='/api/v1/catalog':
            assert req.url.params.get('branchId')==BRANCH
            return httpx.Response(200,json={
                'entities':[{'id':ENTITY,'name':'网关','code':'gateway','parentId':None}],
                'attributes':[],'relations':[],'actions':[],'capabilities':[]})
        raise AssertionError(req.url.path)
    mod=importlib.import_module('app.main')
    original=OpenMindAutoClient
    def client_factory(base_url,username,password,allowed_hosts):
        return original(base_url,username,password,allowed_hosts=allowed_hosts,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mod,'OpenMindAutoClient',client_factory)
    with TestClient(create_app(tmp_path/'state')) as c:
        r=c.put('/api/settings/openmind',json={'base_url':'https://openmind.example',
            'allowed_hosts':['openmind.example'],'auth_mode':'login',
            'username':'service_user','password':'non-real-password'})
        assert r.status_code==200,r.text
        assert not r.json()['has_access_token']
        assert r.json()['has_password']
        r=c.get('/api/settings/openmind/domains')
        assert r.status_code==200,r.text
        assert len(r.json()['items'])==1
        r=c.post('/api/projects',json={'name':'从分支开始','domain_id':DOMAIN,'branch_id':BRANCH})
        assert r.status_code==200,r.text
        assert r.json()['openmind_baseline']['entity_count']==1
        assert counts=={'login':1,'refresh':0}
        conf=c.get('/api/settings/openmind').json()
        assert 'test-access' not in str(conf)
        assert 'non-real-password' not in str(conf)
