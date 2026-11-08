"""OpenMind backend-login contract + branch-first workspace creation."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.openmind import OpenMindError
from app.openmind_auth import OpenMindAutoClient
from app.store import Store

DOMAIN='10000000-0000-4000-8000-000000000001'
BRANCH='10000000-0000-4000-8000-000000000002'
ENTITY='20000000-0000-4000-8000-000000000001'
ATTR='30000000-0000-4000-8000-000000000001'


def login_response(token, expires_in=180, identity=False):
    payload=({'sessionType':'IDENTITY','identityStatus':'PENDING','expiresAt':int(time.time())+180}
             if identity else {'sessionType':'FULL','accessToken':token,
                'expiresAt':int(time.time())+expires_in,'idleTimeoutSeconds':10800,
                'user':{'id':'10000000-0000-4000-8000-000000000005','username':'connector','displayName':'接入账号','role':'user'}})
    return httpx.Response(200,json=payload,headers=[
        ('Set-Cookie','om_refresh=refresh-'+token+'; HttpOnly; Path=/api/v1/auth; SameSite=Lax'),
        ('Set-Cookie','om_csrf=csrf-'+token+'; Path=/; SameSite=Lax')])


def test_backend_login_me_refresh_rotating_cookies_and_parallel_single_flight():
    events=[]
    def handler(req):
        route=req.url.path
        events.append((route,req.headers.get('authorization',''),req.headers.get('x-csrf-token',''),req.headers.get('cookie','')))
        if route.endswith('/login'):
            assert req.headers.get('authorization') is None
            assert json.loads(req.content)=={'username':'connector','password':'not-a-real-password'}
            return login_response('token-one')
        if route.endswith('/me'):
            assert req.headers['authorization']=='Bearer token-one'
            return httpx.Response(200,json={'id':'u','username':'connector','displayName':'接入账号','role':'user'})
        if route.endswith('/refresh'):
            assert req.headers.get('x-csrf-token')=='csrf-token-one'
            assert 'om_refresh=refresh-token-one' in req.headers['cookie']
            assert 'om_csrf=csrf-token-one' in req.headers['cookie']
            return login_response('token-two',expires_in=3600)
        if route.endswith('/domains'):
            assert req.headers['authorization'] in ('Bearer token-one','Bearer token-two')
            return httpx.Response(200,json={'items':[], 'total':0})
        raise AssertionError(route)
    c=OpenMindAutoClient('https://openmind.example','connector','not-a-real-password',
                         transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        assert c.domains()=={'items':[],'total':0}
        assert c.session_account()['username']=='connector'
        c._expires_at=time.time()+1
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses=list(pool.map(lambda _:c.domains(),range(6)))
        assert all(r['items']==[] for r in responses)
        assert sum(path.endswith('/login') for path,*_ in events)==1
        assert sum(path.endswith('/refresh') for path,*_ in events)==1
        assert c.token=='token-two'
    finally:c.close()


def test_identity_session_rejected_even_when_http_200():
    seen=[]
    def handler(req):
        seen.append(req.url.path)
        return login_response('restricted',identity=True)
    c=OpenMindAutoClient('https://openmind.example','connector','pwd',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        with pytest.raises(OpenMindError,match='FULL'):
            c.domains()
        assert seen==['/api/v1/auth/login']
    finally:c.close()


def test_auth_401_does_not_replay_unsafe_post():
    events=[]
    def handler(req):
        events.append((req.method,req.url.path))
        if req.url.path.endswith('/login'):return login_response('token-1')
        if req.url.path.endswith('/me'):return httpx.Response(200,json={'username':'connector'})
        return httpx.Response(401,json={'code':'INVALID_TOKEN'})
    c=OpenMindAutoClient('https://openmind.example','connector','pwd',transport=httpx.MockTransport(handler),allowed_hosts=['openmind.example'])
    try:
        with pytest.raises(OpenMindError,match='不会自动重放'):
            c.request('POST','/api/v1/branches/x/entity',data={'data':{}})
        assert events.count(('POST','/api/v1/branches/x/entity'))==1
    finally:c.close()


def test_login_credentials_encrypt_persist_and_never_echo(tmp_path,monkeypatch):
    for key in ('APP_ENV','OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN','OPENMIND_AUTH_MODE',
                'OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_ALLOWED_HOSTS','OPENMIND_ADMIN_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    with TestClient(create_app(tmp_path/'db')) as c:
        response=c.put('/api/settings/openmind',json={'base_url':'https://openmind.example',
            'auth_mode':'login','allowed_hosts':['openmind.example'], 'username':'connector',
            'password':'TOP-SECRET-ONLY-ON-BACKEND'})
        assert response.status_code==200,response.text
        conf=response.json()
        assert conf['configured'] and conf['has_password'] and conf['auth_mode']=='login'
        assert 'TOP-SECRET-ONLY-ON-BACKEND' not in str(conf)
        assert 'TOP-SECRET-ONLY-ON-BACKEND' not in (tmp_path/'db'/'ontology.sqlite3').read_bytes().decode('latin-1')
    with TestClient(create_app(tmp_path/'db')) as c:
        conf=c.get('/api/settings/openmind').json()
        assert conf['username']=='connector' and conf['has_password']
        assert 'password' not in conf or conf.get('password') is None


def remote():
    return {'entities':[{'id':ENTITY,'name':'人员','code':'person','parentId':None,'description':'人员类型'}],
            'attributes':[{'id':ATTR,'name':'姓名','code':'name','dataType':'string','entityIds':[ENTITY]}],
            'relations':[], 'actions':[], 'capabilities':[]}


def setup_fake(monkeypatch,tmp_path):
    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('OPENMIND_BASE_URL',raising=False)
    monkeypatch.delenv('OPENMIND_ACCESS_TOKEN',raising=False)
    mod=importlib.import_module('app.main')
    scenario={'catalog':remote(),'role':'draft','type':'local','break_read':False,'visible':True}
    class FakeClient:
        def __init__(self,*args,**kwargs):pass
        def domains(self):
            return {'items':[{'id':DOMAIN,'name':'业务本体','code':'business','type':scenario['type'],
                'branches':[{'id':BRANCH,'version':'v_test_1','status':scenario['role'],'branchType':'development'}]}] if scenario['visible'] else [],'total':1}
        def catalog(self,branch):
            assert branch==BRANCH
            if scenario['break_read']:raise OpenMindError('模拟目录读取失败')
            return deepcopy(scenario['catalog'])
        def close(self):pass
    monkeypatch.setattr(mod,'OpenMindClient',FakeClient)
    client=TestClient(create_app(tmp_path/'state'))
    r=client.put('/api/settings/openmind',json={'base_url':'https://openmind.example','allowed_hosts':['openmind.example'],
            'access_token':'demo-read-account'})
    assert r.status_code==200,r.text
    return client,scenario


def test_branch_first_workspace_auto_baseline_and_chat(tmp_path,monkeypatch):
    client,scenario=setup_fake(monkeypatch,tmp_path)
    with client:
        # No project may be created without a branch once OpenMind is configured.
        assert client.post('/api/projects',json={'name':'缺少分支'}).status_code==422
        domains=client.get('/api/settings/openmind/domains').json()['items']
        assert domains[0]['id']==DOMAIN and domains[0]['branches'][0]['selectable']
        response=client.post('/api/projects',json={'name':'增量本体工作空间','domain_id':DOMAIN,'branch_id':BRANCH})
        assert response.status_code==200,response.text
        view=response.json();pid=view['project']['id']
        assert view['openmind_bound']
        assert view['openmind_baseline']['entity_count']==1
        assert view['openmind_baseline']['attribute_count']==1
        assert view['snapshot']['entities'][0]['id']==ENTITY
        assert view['snapshot']['entities'][0]['origin']=='openmind'
        assert any('选择 OpenMind 分支' in m['content'] for m in view['messages'])
        assert client.get(f'/api/projects/{pid}/openmind/baseline?kind=attribute').json()['items'][0]['id']==ATTR
        assert len(client.get('/api/projects').json())==1
        # One remote UUID may be referenced in independent workspaces.
        duplicate=client.post('/api/projects',json={'name':'同分支第二项目','domain_id':DOMAIN,'branch_id':BRANCH})
        assert duplicate.status_code==200,duplicate.text
        other=duplicate.json()['project']['id']
        assert other!=pid
        assert duplicate.json()['snapshot']['entities'][0]['id']==ENTITY
        assert len(client.get('/api/projects').json())==2


def test_branch_readonly_not_selectable_and_rejected(tmp_path,monkeypatch):
    client,scenario=setup_fake(monkeypatch,tmp_path)
    with client:
        scenario['role']='published'
        branch=client.get('/api/settings/openmind/domains').json()['items'][0]['branches'][0]
        assert not branch['selectable']
        r=client.post('/api/projects',json={'name':'禁止写入','domain_id':DOMAIN,'branch_id':BRANCH})
        assert r.status_code==422
        scenario['role']='draft';scenario['visible']=False
        r=client.post('/api/projects',json={'name':'无领域权限','domain_id':DOMAIN,'branch_id':BRANCH})
        assert r.status_code==403
        assert client.get('/api/projects').json()==[]


def test_failed_catalog_leaves_no_half_workspace(tmp_path,monkeypatch):
    client,scenario=setup_fake(monkeypatch,tmp_path)
    with client:
        scenario['break_read']=True
        response=client.post('/api/projects',json={'name':'目录失败','domain_id':DOMAIN,'branch_id':BRANCH})
        assert response.status_code!=200
        assert client.get('/api/projects').json()==[]
        scenario['break_read']=False
        scenario['catalog']['entities'].append({'id':'20000000-0000-4000-8000-000000000002',
            'name':'人员','code':'person2','parentId':None})
        response=client.post('/api/projects',json={'name':'名称冲突','domain_id':DOMAIN,'branch_id':BRANCH})
        assert response.status_code==409
        assert client.get('/api/projects').json()==[]
