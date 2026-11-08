"""Regression tests for end-to-end approval, incremental change and ontology export."""
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph

from app.main import create_app
from app.extraction import extract, ExtractionError


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('API_TOKEN', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    monkeypatch.setenv('APP_ENV', 'development')
    with TestClient(create_app(tmp_path / 'storage')) as app_client:
        yield app_client


def request_ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if 'application/json' in response.headers.get('content-type','') else response


def project(client):
    return request_ok(client.post('/api/projects', json={'name': '网关本体'}))['project']['id']


def upload_and_confirm(client, pid):
    raw = Path(__file__).parents[1].joinpath('examples/gateway-structure.txt').read_bytes()
    request_ok(client.post(f'/api/projects/{pid}/documents',files={'file': ('网关.txt',raw,'text/plain')}))
    summary = request_ok(client.post(f'/api/projects/{pid}/analyze'))
    assert '规则模式' in summary['summary']
    request_ok(client.put(f'/api/projects/{pid}/topic',json={'summary':'网关设备结构','scope':'设备组件与功能模块'}))


def view(client, pid):
    return request_ok(client.get(f'/api/projects/{pid}'))


def approve_all(client, pid, kind):
    data=view(client,pid)
    candidates=[q for q in data['proposals'] if q['kind']==kind and q['status']=='pending']
    for q in candidates:
        request_ok(client.post(f'/api/projects/{pid}/proposals/{q["id"]}/decision',json={'action':'accept'}))
    return candidates


def add_request(client, pid, kind, payload):
    ids=request_ok(client.post(f'/api/projects/{pid}/proposals',json={'kind':kind,'payload':payload}))['created']
    assert ids
    return ids[0]


def decide(client, pid, proposal_id, **kw):
    return request_ok(client.post(f'/api/projects/{pid}/proposals/{proposal_id}/decision',json={'action':'accept',**kw}))


def full_workflow(client):
    pid=project(client)
    upload_and_confirm(client,pid)
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    assert len(approve_all(client,pid,'entity'))>=5
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    assert len(approve_all(client,pid,'attribute'))>=1
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    assert len(approve_all(client,pid,'relation'))>=1
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'REVIEW'}))
    return pid


def test_full_loop_publish_export_reload_restore(client):
    pid=full_workflow(client)
    pre=view(client,pid)
    assert pre['validation']['valid']
    assert pre['snapshot']['entities'] and pre['snapshot']['attributes'] and pre['snapshot']['relations']
    version=request_ok(client.post(f'/api/projects/{pid}/publish',json={'note':'首轮批准'}))
    assert version['published_version']==1
    for fmt, rdf_format in [('ttl','turtle'),('owl','xml'),('jsonld','json-ld')]:
        result=request_ok(client.get(f'/api/projects/{pid}/export?format={fmt}&version=1'))
        graph=Graph().parse(data=result.text,format=rdf_format)
        assert len(graph)>10
    response=request_ok(client.get(f'/api/projects/{pid}/export?format=json'))
    assert response['entities']
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'ENTITY'}))
    new_id=add_request(client,pid,'entity',{'name':'测试部件','kind':'class','description':'仅用于测试'})
    decide(client,pid,new_id)
    diff=request_ok(client.get(f'/api/projects/{pid}/diff'))
    assert len(diff['changes']['entities']['added'])==1
    restored=request_ok(client.post(f'/api/projects/{pid}/releases/1/restore'))
    assert not any(e['name']=='测试部件' for e in restored['snapshot']['entities'])
    assert restored['project']['published_version']==1
    assert len(request_ok(client.get(f'/api/projects/{pid}/audit')))>5


def test_human_updates_deletes_concurrency_and_references(client):
    pid=project(client)
    upload_and_confirm(client,pid)
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    approve_all(client,pid,'entity')
    data=view(client,pid)
    gw=next(e for e in data['snapshot']['entities'] if e['name']=='网关')
    rev=data['project']['revision']
    p=add_request(client,pid,'entity',{'operation':'update','target_id':gw['id'], 'name':'综合网关','kind':'class','description':'人工协商后名称'})
    decide(client,pid,p,expected_revision=rev)
    assert any(e['name']=='综合网关' for e in view(client,pid)['snapshot']['entities'])
    # stale clients cannot accept on top of a newer revision
    p=add_request(client,pid,'entity',{'name':'另一个实体','kind':'class'})
    response=client.post(f'/api/projects/{pid}/proposals/{p}/decision',json={'action':'accept','expected_revision':rev})
    assert response.status_code==409
    request_ok(client.post(f'/api/projects/{pid}/proposals/{p}/decision',json={'action':'reject','note':'不需要'}))
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    approve_all(client,pid,'attribute')
    attr=view(client,pid)['snapshot']['attributes'][0]
    assert any(e['id']==attr['owner_id'] for e in view(client,pid)['snapshot']['entities'])
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    request_ok(client.post(f'/api/projects/{pid}/propose'))
    approve_all(client,pid,'relation')
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    # Block deletion of referenced entities
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'ENTITY'}))
    delete=add_request(client,pid,'entity',{'operation':'delete','target_id':view(client,pid)['snapshot']['relations'][0]['source_id']})
    assert client.post(f'/api/projects/{pid}/proposals/{delete}/decision',json={'action':'accept'}).status_code==409
    request_ok(client.post(f'/api/projects/{pid}/proposals/{delete}/decision',json={'action':'reject'}))
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    delete_attr=add_request(client,pid,'attribute',{'operation':'delete','target_id':attr['id']})
    decide(client,pid,delete_attr)
    assert not any(a['id']==attr['id'] for a in view(client,pid)['snapshot']['attributes'])
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    rel=view(client,pid)['snapshot']['relations'][0]
    update=add_request(client,pid,'relation',{'operation':'update','record_id':rel['id'], 'source_id':rel['source_id'],'target_id':rel['target_id'],'predicate':'hasComponent','description':'关系词修订'})
    decide(client,pid,update)
    assert 'hasComponent' in {x['predicate'] for x in view(client,pid)['snapshot']['relations']}


def test_validation_stage_and_topic_gate(client):
    pid=project(client)
    bad=client.post(f'/api/projects/{pid}/proposals',json={'kind':'entity','payload':{'name':'A'}})
    assert bad.status_code==409
    assert client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}).status_code==409
    upload_and_confirm(client,pid)
    assert client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}).status_code==409
    # Cannot submit a relation during entity stage
    assert client.post(f'/api/projects/{pid}/proposals',json={'kind':'relation','payload':{}}).status_code==409
    self_link=add_request(client,pid,'entity',{'name':'类型A','kind':'class'})
    decide(client,pid,self_link)
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
    request_ok(client.post(f'/api/projects/{pid}/stage',json={'target':'RELATION'}))
    eid=view(client,pid)['snapshot']['entities'][0]['id']
    assert client.post(f'/api/projects/{pid}/proposals',json={'kind':'relation','payload':{'source_id':eid,'target_id':eid,'predicate':'subClassOf'}}).status_code==422


def test_chat_demo_and_duplicate_file(client):
    pid=project(client)
    upload_and_confirm(client,pid)
    reply=request_ok(client.post(f'/api/projects/{pid}/chat',json={'message':'请增加设备属性'}))
    assert '规则演示模式' in reply['reply']
    assert not reply['created']
    assert len([m for m in view(client,pid)['messages'] if m['source']=='chat'])==2
    assert any(m['source']=='action' and '确认本体主题' in m['content'] for m in view(client,pid)['messages'])
    raw=Path(__file__).parents[1].joinpath('examples/gateway-structure.txt').read_bytes()
    assert client.post(f'/api/projects/{pid}/documents',files={'file':('重复.txt',raw,'text/plain')}).status_code==409


def test_local_structured_file_extractors(tmp_path):
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    path=tmp_path/'sample.docx'; d=Document();d.add_paragraph('主板属于设备组件');d.save(path)
    assert '主板' in extract(path)['content']
    path=tmp_path/'sample.xlsx';wb=Workbook();wb.active.append(['设备','网关']);wb.save(path)
    assert '网关' in extract(path)['content']
    path=tmp_path/'sample.pptx';p=Presentation();slide=p.slides.add_slide(p.slide_layouts[6]);box=slide.shapes.add_textbox(0,0,1000000,1000000);box.text='网关硬件结构';p.save(path)
    assert '网关' in extract(path)['content']
    path=tmp_path/'sample.pdf'
    from reportlab.pdfgen import canvas
    # PDF text extraction support; English for font-independent test
    c=canvas.Canvas(str(path));c.drawString(100,700,'Gateway and Mainboard');c.save()
    assert 'Gateway' in extract(path)['content']
    path=tmp_path/'sample.json';path.write_text('{"实体":"网关"}',encoding='utf-8')
    assert '网关' in extract(path)['content']
