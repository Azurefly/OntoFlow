"""v1.0: per-candidate partial success, no all-or-nothing rollback."""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.delenv('LLM_MODEL',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    with TestClient(create_app(tmp_path/'state')) as c:yield c

def ok(r,status=200):
    assert r.status_code==status,r.text
    return r.json()

def bootstrap(c):
    pid=ok(c.post('/api/projects',json={'name':'独立审核测试'}))['project']['id']
    ok(c.post(f'/api/projects/{pid}/documents',files={'file':('a.txt',b'entity data','text/plain')}))
    ok(c.put(f'/api/projects/{pid}/topic',json={'summary':'本体','scope':'范围'}))
    return pid

def create(c,pid,name):
    return ok(c.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':name,'kind':'class'}}))['created'][0]

def state(c,pid):return ok(c.get(f'/api/projects/{pid}'))

def batch(c,pid,ids,action='accept',kind='entity',rev=None,note=''):
    if rev is None:rev=state(c,pid)['project']['revision']
    return c.post(f'/api/projects/{pid}/proposals/batch-decision',json={'kind':kind,'proposal_ids':ids,'action':action,'expected_revision':rev,'note':note})

def test_each_accepted_item_commits_and_messages(client):
    pid=bootstrap(client); ids=[create(client,pid,n) for n in ['路由器','网关','板卡']]
    rev=state(client,pid)['project']['revision']
    r=ok(batch(client,pid,ids,rev=rev))
    assert (r['succeeded'],r['failed'],r['revision'])==(3,0,rev+3)
    assert {x['name'] for x in state(client,pid)['snapshot']['entities']}=={'路由器','网关','板卡'}
    assert sum('用户批量确认' in x['content'] for x in state(client,pid)['messages'])==3
    retry=ok(batch(client,pid,ids))
    assert retry['failed']==3 and retry['succeeded']==0


def test_reject_does_not_increase_revision(client):
    pid=bootstrap(client);ids=[create(client,pid,'不用A'),create(client,pid,'不用B')]
    rev=state(client,pid)['project']['revision']
    r=ok(batch(client,pid,ids,action='reject',note='证据不足'))
    assert r['succeeded']==2 and r['revision']==rev
    assert not state(client,pid)['snapshot']['entities']
    assert all(x['review_note']=='证据不足' and x['status']=='rejected' for x in state(client,pid)['proposals'])


def test_invalid_later_candidate_does_not_rollback_prior_success(client):
    pid=bootstrap(client)
    old=create(client,pid,'旧实体')
    ok(client.post(f'/api/projects/{pid}/proposals/{old}/decision',json={'action':'accept'}))
    target=state(client,pid)['snapshot']['entities'][0]
    update=ok(client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{
        'operation':'update','target_id':target['id'],'name':'同名目标','kind':'class'}}))['created'][0]
    addition=create(client,pid,'同名目标')
    rev=state(client,pid)['project']['revision']
    r=ok(batch(client,pid,[update,addition]))
    assert r['succeeded']==1 and r['failed']==1 and r['revision']==rev+1
    current=state(client,pid)
    assert current['snapshot']['entities'][0]['name']=='同名目标'
    assert {x['id']:x['status'] for x in current['proposals']}[addition]=='pending'


def test_stale_and_missing_id_are_individual_failures(client):
    pid=bootstrap(client); a=create(client,pid,'候选A');b=create(client,pid,'候选B')
    assert ok(batch(client,pid,[a,b],rev=-1))['failed']==2
    assert batch(client,pid,[a,a]).status_code==422
    r=ok(batch(client,pid,[a,'missing',b]))
    assert r['succeeded']==2 and r['failed']==1
    assert {x['status'] for x in state(client,pid)['proposals']}=={'accepted'}


def test_already_reviewed_item_does_not_stop_later_item(client):
    pid=bootstrap(client);a=create(client,pid,'候选A');b=create(client,pid,'候选B')
    ok(client.post(f'/api/projects/{pid}/proposals/{a}/decision',json={'action':'accept'}))
    r=ok(batch(client,pid,[a,b]))
    assert r['succeeded']==1 and r['failed']==1
    assert len(state(client,pid)['snapshot']['entities'])==2


def test_invalid_input_and_missing_scope(client):
    pid=bootstrap(client)
    assert batch(client,pid,[]).status_code==422
    assert batch(client,pid,['x']*201).status_code==422
    a=create(client,pid,'候选A')
    with client.app.state.store.tx() as c:
        c.execute('UPDATE projects SET scope_confirmed=0 WHERE id=?',(pid,))
    r=ok(batch(client,pid,[a]))
    assert r['failed']==1 and state(client,pid)['proposals'][0]['status']=='pending'


def test_property_and_relation_partial_success(client):
    pid=bootstrap(client)
    ok(batch(client,pid,[create(client,pid,'设备'),create(client,pid,'组件')]))
    es={x['name']:x['id'] for x in state(client,pid)['snapshot']['entities']}
    ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    ids=[]
    for name,owner in [('设备编号','设备'),('组件编号','组件')]:
        ids.extend(ok(client.post(f'/api/projects/{pid}/proposals',json={'kind':'attribute',
            'payload':{'name':name,'owner_id':es[owner],'datatype':'string'}}))['created'])
    assert ok(batch(client,pid,ids,kind='attribute'))['succeeded']==2
    ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    ids=[]
    for predicate in ('hasPart','relatedTo'):
        ids.extend(ok(client.post(f'/api/projects/{pid}/proposals',json={'kind':'relation',
            'payload':{'source_id':es['设备'],'target_id':es['组件'],'predicate':predicate}}))['created'])
    assert ok(batch(client,pid,ids,kind='relation'))['succeeded']==2


def test_zero_adoption_can_publish_and_keep_pending(client):
    pid=bootstrap(client)
    proposal=create(client,pid,'不想采纳')
    for stage in ('PROPERTY','RELATION','REVIEW'):
        ok(client.post(f'/api/projects/{pid}/stage',json={'target':stage}))
    result=ok(client.post(f'/api/projects/{pid}/publish',json={'note':'无新增，仅保留基线'}))
    assert result['published_version']==1
    assert any(x['id']==proposal and x['status']=='pending' for x in state(client,pid)['proposals'])


def test_server_pagination(client):
    pid=bootstrap(client)
    for i in range(25):create(client,pid,f'设备_{i:02}')
    one=ok(client.get(f'/api/projects/{pid}/proposals/page?kind=entity&page=1&page_size=10'))
    two=ok(client.get(f'/api/projects/{pid}/proposals/page?kind=entity&page=2&page_size=10'))
    assert one['total']==25 and len(one['items'])==10 and len(two['items'])==10
    assert not ({x['id'] for x in one['items']} & {x['id'] for x in two['items']})
