"""Audit real public business documents without editing the application or supplying model answers.

Run: python -m scripts.audit_business_documents --output /tmp/business-audit
Raw documents are downloaded to output/corpus (not committed). The resulting
report separates bytes/text retention, rule discovery, model routing and API upload.
No production credentials, PostgreSQL or OpenMind are used.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, mimetypes, os, re, subprocess, tempfile, time, traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

BASE_COMMIT='dd0e433655e3be16fc8da3fb5b8274fd9c3108d6'
SAMPLES=[
 {'id':'hospital_his','file':'hospital_his.docx','title':'河南理工大学医院信息化系统项目采购需求','domain':'医院挂号、收费、病历、药品及采购验收','landing':'https://xyy.hpu.edu.cn/info/1031/1667.htm','url':'https://xyy.hpu.edu.cn/system/_content/download.jsp?owner=1852181094&urltype=news.DownloadAttachUrl&wbfileid=74E83283F439518A89974CE74CFC4F02','anchors':['门急诊挂号系统','电子病历','护士工作站','药品管理系统','供应商','维保服务']},
 {'id':'gov_data_quality','file':'gov_data_quality.docx','title':'政务服务数据质量提升和应用项目（2026年）设计咨询服务项目采购需求','domain':'政务数据质量、应用建设及咨询交付','landing':'https://www.sz.gov.cn/szzsj/gkmlpt/content/12/12709/post_12709535.html','url':'https://www.sz.gov.cn/szzsj/attachment/1/1698/1698707/12709535.docx','anchors':['数据质量','政务服务','验收','采购人','合同','数据治理']},
 {'id':'beijing_rail','file':'beijing_rail.pdf','title':'北京市轨道交通运营突发事件应急预案（2024年修订）','domain':'城市轨道应急、预警、指挥、响应及恢复','landing':'https://yjglj.beijing.gov.cn/art/2025/6/19/art_2522_687582.html','url':'https://www.beijing.gov.cn/zhengce/zhengcefagui/202411/W020241120370897690852.pdf','anchors':['现场指挥部','预警信息','信息报送','分级响应','应急演练','医疗救治组']},
 {'id':'sichuan_rail','file':'sichuan_rail.pdf','title':'四川省城市轨道交通运营突发事件应急预案（试行）','domain':'省市多级应急、分类阈值、跨部门职责及附录流程图','landing':'https://www.sc.gov.cn/10462/zfwjts/2022/1/4/73a88c9f2967450b872097e68153e154.shtml','url':'https://www.sc.gov.cn/10462/zfwjts/2022/1/4/73a88c9f2967450b872097e68153e154/files/274196b5e8a347479038b0304f8d595a.PDF','anchors':['省指挥部','运营单位','人员搜救组','响应行动','调查评估','应急响应流程图']},
 {'id':'property_registration','file':'property_registration.html','title':'北京市不动产登记工作规范','domain':'申请、受理、审核、权利登记、抵押、异议及归档','landing':'https://ghzrzyw.beijing.gov.cn/zhengwuxinxi/zcwj/gfxwj/202406/t20240605_3704790.html','url':'https://ghzrzyw.beijing.gov.cn/zhengwuxinxi/zcwj/gfxwj/202406/t20240605_3704790.html','anchors':['申请人','不动产登记簿','抵押权登记','异议登记','查封登记','代理人']},
 {'id':'egov_guide','file':'egov_guide.html','title':'互联网+政务服务技术体系建设指南','domain':'受理审批、电子证照、办件流转、共享交换及评价','landing':'https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/201701/t20170113_2519635.htm','url':'https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/201701/t20170113_2519635.htm','anchors':['申请材料','电子证照','并联审批','办件信息','事项清单','电子归档']},
]

def digest(data):return hashlib.sha256(data).hexdigest()
def compact(text):return re.sub(r'\s+','',str(text))
def write_json(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def code_hashes(root):
 return {p.relative_to(root).as_posix():digest(p.read_bytes()) for directory in ('app','static') for p in (root/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}

def download(sample,corpus):
 import requests
 record={**sample,'retrieved_at':dt.datetime.now(dt.timezone.utc).isoformat()}
 attempts=[]
 for attempt in range(2):
  try:
   with requests.get(sample['url'],timeout=(15,60),stream=True,headers={'User-Agent':'Mozilla/5.0 (public document compatibility audit)','Referer':sample['landing']}) as response:
    response.raise_for_status(); chunks=[]; total=0
    for part in response.iter_content(65536):
     total+=len(part)
     if total>25*1024*1024:raise ValueError('Public test file exceeds 25 MiB limit')
     chunks.append(part)
    raw=b''.join(chunks)
    ext=Path(sample['file']).suffix
    if ext=='.pdf' and not raw.lstrip().startswith(b'%PDF'):raise ValueError('Response is not PDF bytes')
    if ext=='.docx' and not raw.startswith(b'PK'):raise ValueError('Response is not an Office ZIP')
    if not raw:raise ValueError('Empty response')
    (corpus/sample['file']).write_bytes(raw)
    record.update(status='downloaded',bytes=len(raw),sha256=digest(raw),final_url=response.url,content_type=response.headers.get('Content-Type',''),etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'),attempts=attempts)
    return record
  except Exception as exc:attempts.append({'attempt':attempt+1,'error':str(exc)[:450]})
 record.update(status='download_failed',attempts=attempts)
 return record

def reference(path):
 ext=path.suffix.lower(); units=[]; facts={}
 if ext=='.docx':
  with ZipFile(path) as z:
   root=ET.fromstring(z.read('word/document.xml')); ns=root.tag.rsplit('}',1)[0]+'}'
   paragraphs=list(root.iter(ns+'p'));tables=list(root.iter(ns+'tbl'))
   units=[''.join(t.text or '' for t in para.iter(ns+'t')) for para in paragraphs]
   facts={'paragraphs':len(paragraphs),'tables':len(tables),'namespace':ns}
   method='OOXML paragraphs independently read with stdlib ElementTree'
 elif ext=='.pdf':
  proc=subprocess.run(['pdftotext','-layout',str(path),'-'],capture_output=True,text=True,timeout=45)
  if proc.returncode:raise ValueError(proc.stderr[:500])
  units=[line for line in proc.stdout.splitlines() if line.strip()]
  from pypdf import PdfReader
  reader=PdfReader(path);facts={'pages':len(reader.pages),'form_fields':len(reader.get_fields() or {})}
  method='Independent Poppler -layout line extraction (not OCR/diagram semantics)'
 else:
  from bs4 import BeautifulSoup
  soup=BeautifulSoup(path.read_bytes(),'html.parser')
  for node in soup(['script','style','noscript']):node.decompose()
  units=[x for x in soup.get_text('\n',strip=True).splitlines() if x.strip()]
  facts={'tables':len(soup.find_all('table')),'images':len(soup.find_all('img')),'headings':len(soup.find_all(re.compile(r'^h[1-6]$')))}
  method='Independent BeautifulSoup HTML text nodes including navigation'
 # Compare bounded spans, not arbitrary 20k paragraphs. No expected ontology
 # outputs are passed into the application. Layout can cause missing spans.
 normalized=[compact(u) for u in units if len(compact(u))>=4]
 spans=list(dict.fromkeys(u[i:i+160] for u in normalized for i in range(0,len(u),160) if len(u[i:i+160])>=4))
 return spans,facts,method

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
 out=Path(args.output).resolve();corpus=out/'corpus';corpus.mkdir(parents=True,exist_ok=True)
 root=Path(__file__).resolve().parents[1];before=code_hashes(root)
 delta=subprocess.run(['git','diff','--exit-code',BASE_COMMIT,'HEAD','--','app','static'],capture_output=True,text=True)
 if delta.returncode:raise RuntimeError('Application differs from frozen base; abort rather than silently change the baseline')
 write_json(out/'source-hashes-before.json',before)
 records=[]
 with ThreadPoolExecutor(max_workers=4) as pool:
  futures={pool.submit(download,s,corpus):s for s in SAMPLES}
  for future in as_completed(futures):
   record=future.result();records.append(record);print('DOWNLOAD',record['id'],record['status'],flush=True)
 records.sort(key=lambda r:next(i for i,s in enumerate(SAMPLES) if s['id']==r['id']))
 write_json(out/'manifest.json',{'frozen_app_commit':BASE_COMMIT,'samples':records,'selection':'Official publishers; selected business scope before application outputs. Sources are historical public texts, not legal advice or asserted current policy.'})
 for key in list(os.environ):
  if key.startswith(('LLM_','OPENMIND_','MODEL_')) or key in ('DATABASE_URL','API_TOKEN'):os.environ.pop(key,None)
 os.environ.update(APP_ENV='development',ONTOFLOW_DEMO_MODE='false',ASYNC_PARSE='false')
 from app import brain,incremental
 from app.source_understanding import read_source
 from app.extraction import extract
 from app.store import Store
 from app.main import create_app
 from fastapi.testclient import TestClient
 results=[];forbidden=[0]
 def fail_model(*a,**k):forbidden[0]+=1;raise AssertionError('Unexpected actual LLM call in rule-only audit')
 brain.query_llm=fail_model
 for sample in records:
  item={'id':sample['id'],'title':sample['title'],'domain':sample['domain'],'source_status':sample['status']}
  if sample['status']!='downloaded':results.append(item);continue
  path=corpus/sample['file'];raw=path.read_bytes()
  try:
   start=time.perf_counter();parsed=extract(path)
   item['parse']={'seconds':round(time.perf_counter()-start,5),'status':parsed.get('reading_status'), 'characters':len(parsed['content']),'chunks':len(parsed['chunks']),'kinds':dict(Counter(c.get('kind','') for c in parsed['chunks'])),'warnings':parsed.get('warnings',[])}
   spans,facts,method=reference(path);haystack=compact(parsed['content'])
   retained=[v for v in spans if v in haystack];missing=[v for v in spans if v not in haystack]
   item['reference']={'method':method,'facts':facts,'units':len(spans),'retained':len(retained),'missing':len(missing),'ratio':round(len(retained)/len(spans),5) if spans else None,'missing_preview':[s[:100] for s in missing[:8]],'scope':'Text retention only; not semantic recall, page layout or image completeness'}
   joined=''.join(spans)
   item['business_anchor_presence']={term:{'reference':compact(term) in joined,'parsed':compact(term) in haystack} for term in sample['anchors']}
   document={'id':'audit-doc','project_id':'audit-project','filename':path.name,'chunks':json.dumps(parsed['chunks'],ensure_ascii=False)}
   pieces=list(incremental.segments([document]));semantic=0;semantic_chars=0;records_by_kind={k:{} for k in ('entities','attributes','relations')};units=[]
   config={'base_url':'','model':'','api_key':''}
   with tempfile.TemporaryDirectory(prefix='business-rules-') as work:
    store=Store(Path(work));t=time.perf_counter();counts=Counter()
    with brain.using_model(config):
     for piece in pieces:
      source=read_source(piece);remaining=source['definitions'].get('_semantic_text','')
      semantic+=bool(remaining.strip());semantic_chars+=len(remaining)
      value,hit=incremental.cached_clues(store,piece,config,brain.joint_extract_segment)
      counts['cold_hits']+=hit;counts['complete']+=bool(value.get('_complete'));counts['incomplete']+=not value.get('_complete');counts['gaps']+=len(value.get('_unresolved',[]))
      for kind in records_by_kind:
       for candidate in value.get(kind,[]):
        identity=(candidate.get('name',''),) if kind=='entities' else (candidate.get('owner',''),candidate.get('name','')) if kind=='attributes' else (candidate.get('source',''),candidate.get('predicate',''),candidate.get('target',''))
        records_by_kind[kind].setdefault(identity,{'name':candidate.get('name'), 'code':candidate.get('code'),'owner':candidate.get('owner'),'source':candidate.get('source'),'predicate':candidate.get('predicate'),'target':candidate.get('target'),'locator':piece['locator'],'quote':str(candidate.get('quote',''))[:120]})
      units.append({'locator':piece['locator'],'kind':piece.get('kind'),'characters':len(piece['content']),'semantic_text':bool(remaining.strip()),'complete':value.get('_complete'),'gaps':[{k:g.get(k) for k in ('category','reason','recovery')} for g in value.get('_unresolved',[])],'e_a_r':[len(value.get(k,[])) for k in records_by_kind]})
    cold=time.perf_counter()-t;t=time.perf_counter();warm=0
    with brain.using_model(config):
     for piece in pieces:_,hit=incremental.cached_clues(store,piece,config,brain.joint_extract_segment);warm+=hit
    item['rules']={**dict(counts),'segments':len(pieces),'cold_seconds':round(cold,5),'warm_seconds':round(time.perf_counter()-t,5),'warm_hits':warm,'remaining_semantic_units':semantic,'remaining_semantic_characters':semantic_chars,'unique_counts':{k:len(v) for k,v in records_by_kind.items()},'candidates':{k:list(v.values()) for k,v in records_by_kind.items()},'note':'Unreviewed rule suggestions, not gold ontology counts. Zero does not mean there are no concepts.'}
   # Routing test intercepts BEFORE inference and supplies no model output.
   attempts=[0]
   class NeedsModel(Exception):pass
   def probe(*a,**k):attempts[0]+=1;raise NeedsModel()
   brain.query_llm=probe
   with brain.using_model({'base_url':'https://invalid.example/v1','model':'routing-probe','api_key':''}):
    for piece in pieces:
     try:brain.joint_extract_segment(piece)
     except NeedsModel:pass
   brain.query_llm=fail_model
   item['model_routing']={'intercepted_first_requests':attempts[0],'real_requests':0,'semantic_accuracy':'NOT_TESTED: no real provider or human ontology gold set'}
   write_json(out/(sample['id']+'-units.json'),units)
   with tempfile.TemporaryDirectory(prefix='business-api-') as folder:
    with TestClient(create_app(Path(folder))) as client:
     project=client.post('/api/projects',json={'name':'公开业务独立测试 '+sample['id']}).json();pid=project['project']['id']
     t=time.perf_counter();response=client.post(f'/api/projects/{pid}/documents',files={'file':(path.name,raw,mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
     item['api']={'upload_status':response.status_code,'seconds':round(time.perf_counter()-t,5)}
     if response.status_code!=200:item['api']['error']=response.text[:600]
   print('TEST',sample['id'],item['parse']['characters'],item['rules']['unique_counts'],'model_units',attempts[0],flush=True)
  except Exception as exc:
   item['error']={'type':type(exc).__name__,'message':str(exc)[:1200],'traceback':traceback.format_exc()[-2500:]}
   brain.query_llm=fail_model
   print('ERROR',sample['id'],str(exc)[:200],flush=True)
  results.append(item);write_json(out/'results.json',results)
 after=code_hashes(root);write_json(out/'source-hashes-after.json',after)
 summary={'frozen_application_commit':BASE_COMMIT,'test_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'selected':len(SAMPLES),'downloaded':sum(s['status']=='downloaded' for s in records),'parsed':sum('parse' in r for r in results),'api_statuses':dict(Counter(str(r.get('api',{}).get('upload_status','not_run')) for r in results)),'application_unchanged':before==after,'unexpected_model_calls':forbidden[0],'real_llm_requests':0,'real_pg':False,'real_openmind':False,'results_file':'results.json'}
 write_json(out/'summary.json',summary)
 lines=['# 复杂真实业务文档测试（2026-11-08）','',f'冻结应用：`{BASE_COMMIT}`。本轮不修改业务算法，不提供模型预设回答。','', '| 文档 | 文件/读取规模 | 上传 | 规则 E/A/R（未经审核） | 仍需语义处理单元 |','|---|---|---|---|---|']
 for r in results:
  p=r.get('parse',{});rule=r.get('rules',{});c=rule.get('unique_counts',{})
  lines.append(f"| {r['title']} | {p.get('characters','未读')} 字符 / {p.get('chunks','—')} 单元 | {r.get('api',{}).get('upload_status','未执行')} | {c.get('entities','—')}/{c.get('attributes','—')}/{c.get('relations','—')} | {rule.get('remaining_semantic_units','—')} |")
 lines+=['','## 验证边界','这些是公开机构业务文件，不是格式测试夹具。来源版本按下载字节冻结；不主张内容为现行政策，也不提供法律/医疗意见。','未调用真实大模型，不能将规则候选计数或文字保留率称作本体语义准确率。任务路由探针只在请求前终止，没有注入模型答案。','原文中的城市、机构、项目和人员实例不应直接作为本体类型；候选仍需按建模范围和证据审核。未修改算法来适配本批输入。','', '详见 manifest.json（来源与SHA256）、results.json（逐文件结果）、*-units.json（逐单元完整性和问题）。','原始文档仅位于短期 CI artifact 的 corpus/；不提交到源码仓库。下载失败保留，不静默替换样本。']
 (out/'REPORT.zh-CN.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
 print(json.dumps(summary,ensure_ascii=False,indent=2))
 if before!=after or forbidden[0]:raise SystemExit('Audit isolation/integrity failed')

if __name__=='__main__':main()
