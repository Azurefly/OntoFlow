"""Mock the OpenAI-compatible model adapter; does not call a real provider."""
from fastapi.testclient import TestClient
from app import brain
from app.main import create_app


def test_model_summaries_proposals_and_chat(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_MODEL','stub-model')
    monkeypatch.setenv('LLM_BASE_URL','http://example.invalid/v1')
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    calls=[]
    def mock_model(system,user):
        calls.append(system)
        if '内容概要' in system: return {'summary':'网关设备包括主板','scope':'网关构成','questions':['是否包含通信协议？']}
        if '本体工程师' in system:
            return {'proposals':[{'kind':'entity','payload':{'name':'主板','kind':'class','description':'组件'},'evidence':[]}]}
        return {'reply':'建议保留“主板”类，并确认所属部件层级。','proposals':[]}
    monkeypatch.setattr(brain,'query_llm',mock_model)
    with TestClient(create_app(tmp_path/'db')) as c:
        pid=c.post('/api/projects',json={'name':'模拟模型项目'}).json()['project']['id']
        assert c.post(f'/api/projects/{pid}/documents',files={'file':('a.txt','文档描述：网关和主板'.encode(),'text/plain')}).status_code==200
        result=c.post(f'/api/projects/{pid}/analyze').json()
        assert result['summary']=='网关设备包括主板'
        assert c.put(f'/api/projects/{pid}/topic',json={'summary':result['summary'],'scope':result['scope']}).status_code==200
        prop=c.post(f'/api/projects/{pid}/propose').json()
        assert len(prop['created'])==1
        # Before approval the formal ontology is still empty.
        v=c.get(f'/api/projects/{pid}').json()
        assert v['engine']['mode']=='LLM'
        assert v['snapshot']['entities']==[]
        chat=c.post(f'/api/projects/{pid}/chat',json={'message':'这个结构合理吗？'}).json()
        assert '主板' in chat['reply']
        assert len([m for m in c.get(f'/api/projects/{pid}').json()['messages'] if m['source']=='chat'])==2
        assert any(m['source']=='action' for m in c.get(f'/api/projects/{pid}').json()['messages'])
        assert len(calls)==3
