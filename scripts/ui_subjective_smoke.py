"""Local browser smoke test of modeling-intent and provenance UI.

Optional Playwright + Chromium required. Uses synthetic library-domain data and
an in-process FastAPI TestClient. No real model, database server or OpenMind.
Run: python -m scripts.ui_subjective_smoke --output /path/to/screenshots
"""
from __future__ import annotations
import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--chromium',default='/usr/bin/chromium');args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ontoflow-browser-v140-') as td:
        for key in ('DATABASE_URL','API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY','OPENMIND_BASE_URL',
                    'OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_ACCESS_TOKEN','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY'):
            os.environ.pop(key,None)
        os.environ.update(DATA_DIR=td,ONTOFLOW_DEMO_MODE='true',APP_ENV='development',STRUCTURED_EXTRACTION='1')
        from fastapi.testclient import TestClient
        from app.main import create_app
        from playwright.sync_api import sync_playwright
        with TestClient(create_app(Path(td)/'state')) as client:
            view=client.post('/api/projects',json={'name':'图书借阅 · 非标准材料验收'}).json()
            pid=view['project']['id']
            content='实体：图书、借阅记录、自然人\n属性：图书.书名：string\n关系：自然人 borrows 图书'
            response=client.post(f'/api/projects/{pid}/documents',files={'file':('业务随记.txt',content.encode(),'text/plain')})
            assert response.status_code==200,response.text
            response=client.put(f'/api/projects/{pid}/topic',json={'summary':'借阅业务中的通用类型与字段','scope':'图书借阅本体，不录入读者实例'})
            assert response.status_code==200,response.text
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,executable_path=args.chromium,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1540,'height':1000},device_scale_factor=1)
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                # Render local assets without network navigation. This also
                # works under a browser policy that denies all external URLs.
                # Fetch is bridged only to this in-process temporary test app.
                root=Path(__file__).resolve().parents[1]
                def backend(request):
                    r=client.request(request.get('method','GET'),request['url'],
                                     content=request.get('body'),headers=request.get('headers',{}))
                    return {'status':r.status_code,'body':r.text,
                            'type':r.headers.get('content-type','application/json')}
                page.expose_function('localTestBackend',backend)
                html=(root/'static/index.html').read_text()
                html=re.sub(r'<link[^>]+href="/static/app.css[^>]+>', '',html)
                html=re.sub(r'<script[^>]+src="/static/app.js[^>]+></script>', '',html)
                page.set_content(html)
                page.add_style_tag(content=(root/'static/app.css').read_text())
                page.evaluate("""() => {
                    const values=new Map();
                    Object.defineProperty(window,'sessionStorage',{value:{getItem:k=>values.get(k)||null,
                        setItem:(k,v)=>values.set(k,String(v)),removeItem:k=>values.delete(k)}});
                    window.fetch=async (url,options={}) => {
                        const result=await window.localTestBackend({url:String(url),method:options.method||'GET',
                            headers:options.headers||{},body:options.body||null});
                        return new Response(result.body,{status:result.status,
                            headers:{'content-type':result.type}});
                    };
                }""")
                page.add_script_tag(content=(root/'static/app.js').read_text())
                page.wait_for_function('typeof S!=="undefined" && S.projects.length>0',timeout=5000)
                page.locator('[data-project]').first.click()
                page.wait_for_function('S.view!==null',timeout=5000)
                page.locator('#modelingIntentBtn').click()
                page.locator('#intentGoal').fill('以图书及借阅流程为主，不为材料中的个人生成实例')
                page.locator('#intentExclude').fill('自然人')
                page.locator('#intentRole').select_option('relations')
                page.locator('#intentInstructions').fill('读者属于人工指定的业务角色，后续再协商是否需要独立建类。')
                page.locator('#intentNote').fill('先按资料管理范围构建，人员定义暂缓。')
                page.screenshot(path=str(output/'modeling-intent.png'),full_page=True)
                page.get_by_role('button',name='确认并保存用户决定').click()
                page.wait_for_timeout(250)
                stored=client.get(f'/api/projects/{pid}/modeling-context').json()
                assert stored['context']['version']==1 and stored['context']['exclude_types']==['自然人']
                response=client.post(f'/api/projects/{pid}/jobs/start?kind=propose');assert response.status_code==202,response.text
                jid=response.json()['job']['id']
                for _ in range(160):
                    state=client.get(f'/api/projects/{pid}/jobs/{jid}').json()['job']['state']
                    if state not in ('queued','running'):break
                    time.sleep(.02)
                assert state=='succeeded',state
                page.evaluate('(id)=>loadProject(id)',pid)
                page.wait_for_timeout(250)
                page.get_by_text('原文明示',exact=False).first.wait_for(timeout=3000)
                page.screenshot(path=str(output/'candidate-provenance.png'),full_page=True)
                items=client.get(f'/api/projects/{pid}/proposals/page?kind=entity').json()['items']
                assert {p['payload']['name'] for p in items}=={'图书','借阅记录'}
                assert all(p['basis']['kind']=='source_explicit' for p in items)
                page.locator('#modelingIntentBtn').click()
                page.locator('#intentGoal').fill('重新考虑将人员角色纳入建模')
                page.get_by_role('button',name='确认并保存用户决定').click()
                page.wait_for_timeout(250)
                page.get_by_role('button',name='按当前意图复核').first.wait_for(timeout=3000)
                page.screenshot(path=str(output/'stale-candidate-review.png'),full_page=True)
                assert not errors,errors
                browser.close()
                result={'browser':'Chromium','model':'not called','backend':'in-process FastAPI + temporary SQLite',
                        'checks':{'save_intent':True,'source_labels':True,'exact_scope_filter':True,
                                  'stale_candidate_review':True,'js_errors':errors},
                        'screenshots':['modeling-intent.png','candidate-provenance.png','stale-candidate-review.png']}
                (output/'browser-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
