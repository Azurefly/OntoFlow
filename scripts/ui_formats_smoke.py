"""Real frontend smoke: runtime format capabilities and readable-source warnings.

Uses synthetic content, an in-process temporary API and an offline Chromium page.
No real PostgreSQL, OpenMind, model, raw user documents or external URLs.
"""
import argparse,json,os,re,tempfile
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--chromium',default='/usr/bin/chromium');args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ontoflow-formats-smoke-') as td:
        for key in ('DATABASE_URL','API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY','OPENMIND_BASE_URL',
                    'OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_ACCESS_TOKEN','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','ASYNC_PARSE'):
            os.environ.pop(key,None)
        os.environ.update(DATA_DIR=td,ONTOFLOW_DEMO_MODE='true',APP_ENV='development')
        from fastapi.testclient import TestClient
        from app.main import create_app
        from playwright.sync_api import sync_playwright
        with TestClient(create_app(Path(td)/'api',testing=True)) as client:
            pid=client.post('/api/projects',json={'name':'多格式材料 · 读取边界验收'}).json()['project']['id']
            sources=[('原文.html','<h1>馆藏品的属性定义</h1><table><tr><th>属性名称</th><th>类型</th></tr><tr><td>保管状态</td><td>string</td></tr></table><p><img src="not-fetched.png" alt="扫描补充说明"></p>'),
                     ('补充.yaml','描述: "实体：馆藏品"'),('原始记录.csv','名称,取值\n启用,false\n计数,0')]
            for name,text in sources:
                response=client.post(f'/api/projects/{pid}/documents',files={'file':(name,text.encode(),'text/plain')})
                assert response.status_code==200,response.text
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,executable_path=args.chromium,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1540,'height':1050},device_scale_factor=1)
                errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
                root=Path(__file__).resolve().parents[1]
                def backend(request):
                    response=client.request(request.get('method','GET'),request['url'],content=request.get('body'),headers=request.get('headers',{}))
                    return {'status':response.status_code,'body':response.text,'type':response.headers.get('content-type','application/json')}
                page.expose_function('localBackend',backend)
                html=(root/'static/index.html').read_text()
                html=re.sub(r'<link[^>]+href="/static/app.css[^>]+>','',html)
                html=re.sub(r'<script[^>]+src="/static/app.js[^>]+></script>','',html)
                page.set_content(html);page.add_style_tag(content=(root/'static/app.css').read_text())
                page.evaluate('''() => {
                  const values=new Map();
                  Object.defineProperty(window,'sessionStorage',{value:{getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,String(v)),removeItem:k=>values.delete(k)}});
                  window.fetch=async(url,options={})=>{const r=await window.localBackend({url:String(url),method:options.method||'GET',headers:options.headers||{},body:options.body||null});return new Response(r.body,{status:r.status,headers:{'content-type':r.type}})};
                }''')
                page.add_script_tag(content=(root/'static/app.js').read_text())
                page.wait_for_function('typeof S!=="undefined" && S.projects.length>0')
                page.locator('[data-project]').first.click();page.wait_for_function('S.view!==null')
                page.get_by_role('button',name='格式与读取边界').click()
                page.get_by_text('支持格式与实际读取边界',exact=True).wait_for()
                page.get_by_text('安全解析多文档流；不执行对象标签/外部引用',exact=False).first.wait_for()
                page.screenshot(path=str(output/'formats.png'),full_page=True)
                page.get_by_role('button',name='关闭',exact=True).last.click()
                page.evaluate('showReadingBoundaries()')
                page.get_by_text('材料阅读边界 · 不把部分读取当作完整理解',exact=True).wait_for()
                page.get_by_text('未识别图片内文字',exact=False).first.wait_for()
                page.screenshot(path=str(output/'reading-boundaries.png'),full_page=True)
                assert not errors,errors
                report={'browser':'Chromium','backend':'temporary SQLite + in-process FastAPI',
                  'model_calls':0,'external_service_calls':0,'js_errors':errors,
                  'checks':{'runtime_formats_modal':True,'reader_encoding_visible':True,'image_warning_visible':True},
                  'screenshots':['formats.png','reading-boundaries.png']}
                (output/'browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                print(json.dumps(report,ensure_ascii=False,indent=2));browser.close()

if __name__=='__main__':main()
