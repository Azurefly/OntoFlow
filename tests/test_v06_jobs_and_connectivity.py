"""v0.6 regression: durable progress, click-to-chat decisions, diagnostics."""
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.openmind import OpenMindClient, OpenMindError
from app import brain


@pytest.fixture(autouse=True)
def no_cloud_settings(monkeypatch):
    for key in ('API_TOKEN','LLM_BASE_URL','LLM_MODEL','OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN',
                'OPENMIND_ALLOWED_HOSTS','APP_ENV','MODEL_ADMIN_TOKEN','OPENMIND_ADMIN_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('APP_ENV','development')


def wait_job(c,pid,jid):
    for _ in range(200):
        r=c.get(f'/api/projects/{pid}/jobs/{jid}')
        assert r.status_code==200,r.text
        job=r.json()['job']
        if job['state'] not in ('queued','running'):
            return job
        time.sleep(.01)
    raise AssertionError('Background job did not finish in 2 seconds')


def test_rule_job_progress_summary_and_entity_accepted_action_history(tmp_path):
    with TestClient(create_app(tmp_path/'data')) as c:
        pid=c.post('/api/projects',json={'name':'状态测试'}).json()['project']['id']
        data='本体示例\n实体：网关、主板\n说明：结构'.encode('utf8')
        c.post(f'/api/projects/{pid}/documents',files={'file':('demo.txt',data,'text/plain')}).raise_for_status()
        started=c.post(f'/api/projects/{pid}/jobs/start?kind=summary')
        assert started.status_code==202,started.text
        job=wait_job(c,pid,started.json()['job']['id'])
        assert job['state']=='succeeded',job
        assert job['completed_units']==job['total_units'] or job['phase']=='complete'
        view=c.get(f'/api/projects/{pid}').json()
        assert view['project']['candidate_summary']
        assert view['jobs_latest']['summary']['state']=='succeeded'
        assert any(m['source']=='action' and '用户点击生成' in m['content'] for m in view['messages'])
        scope=view['project']['candidate_scope']
        summary=view['project']['candidate_summary']
        assert c.put(f'/api/projects/{pid}/topic',json={'summary':summary,'scope':scope}).status_code==200
        created=c.post(f'/api/projects/{pid}/jobs/start?kind=propose')
        assert created.status_code==202,created.text
        job=wait_job(c,pid,created.json()['job']['id'])
        assert job['state']=='succeeded',job
        view=c.get(f'/api/projects/{pid}').json()
        assert len([p for p in view['proposals'] if p['status']=='pending'])>=2
        assert view['jobs_latest']['ENTITY']['state']=='succeeded'
        coverage=c.get(f'/api/projects/{pid}/coverage?stage=ENTITY').json()
        assert coverage['processed']==coverage['total']==1
        assert coverage['batches'][0]['pending_created']>=2
        batch=c.get(f'/api/projects/{pid}/coverage?stage=ENTITY&batch=1').json()
        assert '实体：网关、主板' in batch['text']
        assert any(m['source']=='action' and '确认本体主题' in m['content'] for m in view['messages'])
        p=next(p for p in view['proposals'] if p['status']=='pending')
        decided=c.post(f'/api/projects/{pid}/proposals/{p["id"]}/decision',json={
             'action':'accept','expected_revision':view['project']['revision']})
        assert decided.status_code==200,decided.text
        msgs=decided.json()['messages']
        assert any(m['source']=='action' and '用户确认实体候选' in m['content'] for m in msgs)
        assert any(m['source']=='action' and m['role']=='assistant' and '通过人工审核' in m['content'] for m in msgs)


def test_background_summary_failure_is_visible_and_retained(tmp_path,monkeypatch):
    def failing(*a,**kw):
        raise ValueError('模拟模型服务拒绝长文档')
    monkeypatch.setattr(brain,'analyze',failing)
    with TestClient(create_app(tmp_path/'data')) as c:
        pid=c.post('/api/projects',json={'name':'故障可见性'}).json()['project']['id']
        c.post(f'/api/projects/{pid}/documents',files={'file':('demo.md',b'text','text/plain')}).raise_for_status()
        r=c.post(f'/api/projects/{pid}/jobs/start?kind=summary')
        assert r.status_code==202
        job=wait_job(c,pid,r.json()['job']['id'])
        assert job['state']=='failed' and '模拟模型服务拒绝长文档' in job['error']
        assert c.get(f'/api/projects/{pid}').json()['jobs_latest']['summary']['state']=='failed'


def test_connecterror_does_not_claim_write_status_uncertain():
    def fail_connect(request):
        raise httpx.ConnectError('mock connection refused')
    cli=OpenMindClient('http://localhost:18888','secret',transport=httpx.MockTransport(fail_connect))
    try:
        with pytest.raises(OpenMindError) as err:
            cli.types()
        msg=str(err.value)
        assert '无法建立连接' in msg and 'host.docker.internal' in msg
        assert '结果可能未确定' not in msg
        assert '回读 catalog' not in msg
    finally:
        cli.close()


def test_timeout_read_vs_write_have_distinct_recovery():
    def fail_read(request):
        raise httpx.ReadTimeout('mock slow API')
    cli=OpenMindClient('https://target.example','secret',transport=httpx.MockTransport(fail_read))
    try:
        with pytest.raises(OpenMindError) as err:
            cli.types()
        assert '只读请求' in str(err.value) and '回读' not in str(err.value)
        with pytest.raises(OpenMindError) as err:
            cli.request('POST','/api/v1/branches/example/entity',data={'data':{}})
        assert '回读目标分支 catalog' in str(err.value)
    finally:
        cli.close()


def test_http_status_401_and_404_have_actionable_messages():
    for code,expected in [(401,'访问令牌'),(404,'API 路径不存在')]:
        cli=OpenMindClient('https://target.example','secret',
                           transport=httpx.MockTransport(lambda req:httpx.Response(code)))
        try:
            with pytest.raises(OpenMindError) as err:cli.types()
            assert expected in str(err.value)
        finally:cli.close()


def test_dense_candidate_batch_does_not_falsely_report_complete(monkeypatch):
    monkeypatch.setenv('LLM_BASE_URL','https://llm.example/v1')
    monkeypatch.setenv('LLM_MODEL','mock')
    def always_more(system,user):
        import json
        page=json.loads(user)['page']
        return {'proposals':[{'kind':'entity','payload':{'name':f'概念{page}','kind':'class','description':''},'evidence':[]}],
                'has_more':True}
    monkeypatch.setattr(brain,'query_llm',always_more)
    docs=[{'chunks':'[{"text":"实体：密集对象","locator":"p1"}]','id':'doc1','filename':'d.txt'}]
    with pytest.raises(brain.ModelFormatError,match='仍有未完成候选'):
        brain.proposal_batches('ENTITY',docs,{'entities':[],'attributes':[],'relations':[]},'密集对象',max_batches=1)
