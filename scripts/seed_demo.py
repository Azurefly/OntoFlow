"""Seed a fully-reviewed gateway ontology for a local demonstration.
Run from project root: python -m scripts.seed_demo
"""
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app


def need(response):
    if response.status_code >= 400:
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: {response.status_code} {response.text}')
    return response.json()


def seed():
    with TestClient(create_app()) as client:
        project=need(client.post('/api/projects',json={'name':'【演示】网关设备本体'}))['project']['id']
        source=Path(__file__).parent.parent / 'examples' / 'gateway-structure.txt'
        need(client.post(f'/api/projects/{project}/documents',files={'file':(source.name,source.read_bytes(),'text/plain')}))
        need(client.post(f'/api/projects/{project}/analyze'))
        need(client.put(f'/api/projects/{project}/topic',json={'summary':'网关设备的主要组成、接口和功能模块。','scope':'以网关设备为核心，构建设备与内部组件、模块的分类及组成关系。'}))
        for stage,kind,next_stage in [('ENTITY','entity','PROPERTY'),('PROPERTY','attribute','RELATION'),('RELATION','relation','REVIEW')]:
            need(client.post(f'/api/projects/{project}/propose'))
            v=need(client.get(f'/api/projects/{project}'))
            for q in v['proposals']:
                if q['status']=='pending' and q['kind']==kind:
                    need(client.post(f'/api/projects/{project}/proposals/{q["id"]}/decision',json={'action':'accept'}))
            need(client.post(f'/api/projects/{project}/stage',json={'target':next_stage}))
        result=need(client.post(f'/api/projects/{project}/publish',json={'note':'演示样例：人工逐项确认'}))
        v=need(client.get(f'/api/projects/{project}'))
        print('项目ID:',project)
        print('已发布版本:',result['published_version'])
        print('实体/属性/关系:',len(v['snapshot']['entities']),len(v['snapshot']['attributes']),len(v['snapshot']['relations']))
        print('请启动 Web 服务并选择【演示】网关设备本体。')


if __name__ == '__main__':
    seed()
