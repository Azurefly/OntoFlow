"""Independent fresh-internet-corpus audit; no real LLM, PG or OpenMind calls.

Usage:
 python -m scripts.audit_external_documents --corpus /path/to/collected-public-fixtures --output reports/external

No extraction expected-counts are copied from BC/previous examples. Parsing,
source retention checks, rule-only discovery and upload API are reported
separately. Local lexical anchors are not a semantic precision/recall measure.
"""
from __future__ import annotations
import argparse
from collections import Counter
import contextlib
import datetime as dt
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import tempfile
import time
import traceback
from xml.etree import ElementTree as ET
from zipfile import ZipFile

# Explicitly isolate this process from operator configuration.
for key in ('DATABASE_URL','OPENMIND_BASE_URL','OPENMIND_USERNAME','OPENMIND_PASSWORD',
            'OPENMIND_ACCESS_TOKEN','OPENMIND_AUTH_MODE','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY',
            'API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY'):
    os.environ.pop(key, None)
os.environ.update(APP_ENV='development', ONTOFLOW_DEMO_MODE='false', ASYNC_PARSE='false')

from app import brain, incremental
from app.extraction import extract
from app.store import Store
from app.main import create_app
from fastapi.testclient import TestClient

NAMESPACES = {
 'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
 'ws': 'http://purl.oclc.org/ooxml/wordprocessingml/main',
 'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
 'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
}

def compact(value):
    return re.sub(r'\s+', '', str(value))

def anchor_check(path, parsed):
    """Reference reader deliberately independent of OntoFlow's extraction code."""
    strings=[]; method='none'; facts={}; ext=path.suffix.lower()
    if ext in ('.docx','.pptx','.xlsx'):
        method='stdlib_zip_xml_source_text'
        with ZipFile(path) as archive:
            if ext=='.docx':
                root=ET.fromstring(archive.read('word/document.xml'))
                strings=[''.join(e.itertext()) for e in root.iter()
                         if e.tag in ('{'+NAMESPACES['w']+'}t','{'+NAMESPACES['ws']+'}t')]
                facts['word_namespace']='strict' if NAMESPACES['ws'] in root.tag else 'transitional'
            elif ext=='.pptx':
                slides=sorted(n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',n))
                facts['slide_xml_count']=len(slides)
                for name in slides:
                    root=ET.fromstring(archive.read(name))
                    strings.extend(e.text or '' for e in root.iter('{'+NAMESPACES['a']+'}t'))
            else:
                if 'xl/sharedStrings.xml' in archive.namelist():
                    root=ET.fromstring(archive.read('xl/sharedStrings.xml'))
                    strings=[''.join(t.text or '' for t in e.iter('{'+NAMESPACES['x']+'}t'))
                             for e in root.iter('{'+NAMESPACES['x']+'}si')]
                for name in archive.namelist():
                    if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',name):
                        root=ET.fromstring(archive.read(name))
                        strings.extend(e.text or '' for e in root.iter('{'+NAMESPACES['x']+'}t'))
                facts['shared_and_inline_string_count']=len(strings)
    elif ext=='.pdf':
        method='poppler_pdftotext_independent_engine'
        proc=subprocess.run(['pdftotext',str(path),'-'],capture_output=True,text=True,timeout=20,check=False)
        if proc.returncode:
            return {'method':method,'error':proc.stderr[:350]}
        strings=[x.strip() for x in proc.stdout.splitlines() if x.strip()]
    elif ext in ('.txt','.rst','.sql','.md','.csv','.tsv'):
        method='raw_utf8_line_or_csv_cell'
        text=path.read_text(encoding='utf-8-sig')
        if ext in ('.csv','.tsv'):
            import csv
            rows=list(csv.reader(text.splitlines(),delimiter='\t' if ext=='.tsv' else ','))
            facts['independent_records_including_header']=len(rows)
            facts['independent_column_widths']=sorted({len(r) for r in rows})
            strings=[v for row in rows for v in row if v.strip()]
        else:
            strings=[x.strip() for x in text.splitlines() if x.strip()]
    elif ext=='.xml':
        method='stdlib_elementtree_text_and_attributes'
        root=ET.fromstring(path.read_bytes())
        strings=list(root.itertext())+[v for el in root.iter() for v in el.attrib.values()]
        facts['root_tag']=root.tag
    elif ext=='.eml':
        method='stdlib_email_body_reference'
        msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        parts=[p for p in msg.walk() if p.get_content_type()=='text/plain' and p.get_content_disposition()!='attachment']
        strings=[line.strip() for p in parts for line in p.get_content().splitlines() if line.strip()]
        facts['mime_parts']=sum(1 for _ in msg.walk())
    elif ext in ('.json','.yaml','.yml','.jsonl','.ndjson'):
        method='independent_structure_inventory_only'
        if ext in ('.yaml','.yml'):
            import yaml
            objects=list(yaml.safe_load_all(path.read_text()))
        elif ext in ('.jsonl','.ndjson'):
            objects=[json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        else:objects=[json.loads(path.read_text())]
        facts['document_or_record_count']=len(objects)
        def leaves(x):
            if isinstance(x,dict):
                for k,v in x.items():
                    yield str(k)
                    yield from leaves(v)
            elif isinstance(x,list):
                for v in x:yield from leaves(v)
            elif x is not None:yield str(x)
        # Schema summarization need not preserve every concrete instance value.
        facts['scalar_and_key_count']=sum(1 for o in objects for _ in leaves(o))
        return {'method':method,'status':'inventory_only_not_full_retention','facts':facts}
    else:
        return {'method':'manual_or_unavailable','status':'not_measured'}
    # All distinct useful strings are checked, not just convenient first lines.
    unique=list(dict.fromkeys(compact(s) for s in strings if len(compact(s))>=3))
    haystack=compact(parsed['content'])
    kept=[s for s in unique if s in haystack]
    missing=[s for s in unique if s not in haystack]
    return {'method':method,'source_units':len(unique),'retained_units':len(kept),
            'missing_units':len(missing),'retention_fraction':round(len(kept)/len(unique),5) if unique else None,
            'missing_unit_sha256':[hashlib.sha256(s.encode()).hexdigest() for s in missing[:12]],
            'facts':facts,
            'scope':'Lexical retention of readable source text; not ontology accuracy and not visual/layout completeness.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--corpus',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    root=Path(args.corpus).resolve();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((root/'manifest.json').read_text())
    source_root=Path(__file__).resolve().parents[1]
    source_hashes={p.relative_to(source_root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in source_root.rglob('*') if p.is_file() and
                   p.suffix in ('.py','.js','.css','.html') and '__pycache__' not in p.parts}
    forbidden=[0]
    original_llm=brain.query_llm
    def no_model(*a,**k):
        forbidden[0]+=1
        raise AssertionError('Real model invocation forbidden in this independent rule-only audit')
    brain.query_llm=no_model
    results=[]
    started=time.perf_counter()
    for sample in manifest['samples']:
        path=(root/sample['file']).resolve()
        if root not in path.parents:raise ValueError('unsafe fixture path')
        data=path.read_bytes()
        item={**sample,'checks':{},'parse':{},'discovery':{},'api':{}}
        item['checks']['sha256_matches']=hashlib.sha256(data).hexdigest()==sample['sha256']
        item['checks']['git_blob_matches']=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==sample['git_blob_sha']
        if not all(item['checks'].values()):raise AssertionError('Fixture integrity failure '+sample['id'])
        try:
            t=time.perf_counter();parsed=extract(path)
            item['parse']={'status':'partial' if parsed['warnings'] else 'read',
                'seconds':round(time.perf_counter()-t,6),'chunks':len(parsed['chunks']),
                'text_characters':len(parsed['content']),'chunk_kinds':dict(Counter(x.get('kind','prose') for x in parsed['chunks'])),
                'warnings':parsed['warnings'],'reader_status':parsed['reading_status']}
            try:item['source_retention']=anchor_check(path,parsed)
            except Exception as exc:item['source_retention']={'status':'oracle_error','error':str(exc)[:500]}
            document={'id':'external-doc','project_id':'external-project','filename':path.name,
                      'chunks':json.dumps(parsed['chunks'],ensure_ascii=False)}
            with tempfile.TemporaryDirectory(prefix='external-audit-') as work:
                store=Store(Path(work))
                totals=Counter();unique={k:set() for k in ('entities','attributes','relations')};errors=[]
                t=time.perf_counter()
                config={'base_url':'','model':'','api_key':''}
                with brain.using_model(config):
                    for unit in incremental.segments([document]):
                        try:
                            clues,hit=incremental.cached_clues(store,unit,config,brain.joint_extract_segment)
                            totals['units']+=1;totals['cold_cache_hits']+=hit
                            totals['completed_units']+=bool(clues.get('_complete'))
                            totals['unresolved']+=len(clues.get('_unresolved',[]))
                            for kind in unique:
                                totals[kind]+=len(clues[kind])
                                for clue in clues[kind]:
                                    identity=tuple(clue.get(k,'') for k in ({'entities':('name',),'attributes':('owner','name'),'relations':('source','predicate','target')}[kind]))
                                    unique[kind].add(identity)
                        except Exception as exc:
                            errors.append({'locator':unit.get('locator'),'error_type':type(exc).__name__,'error':str(exc)[:300]})
                item['discovery']={'mode':'RULE_DEMO_no_real_LLM','status':'error' if errors else 'needs_semantic_review' if totals['unresolved'] else 'rule_pass_finished',
                    'seconds':round(time.perf_counter()-t,6),'counters':dict(totals),
                    'unique_counts':{k:len(v) for k,v in unique.items()},'unit_errors':errors,
                    'meaning':'Zero candidates is NOT proof that the source contains no ontology concepts; semantic LLM accuracy was not tested.'}
        except Exception as exc:
            item['parse']={'status':'rejected','error_type':type(exc).__name__,'error':str(exc)[:800]}
        # Separate actual upload endpoint test, no remote credentials.
        with tempfile.TemporaryDirectory(prefix='external-api-') as work:
            try:
                with TestClient(create_app(Path(work)),raise_server_exceptions=False) as client:
                    response=client.post('/api/projects',json={'name':'外部文件测试-'+sample['id']})
                    if response.status_code!=200:
                        item['api']={'create_status':response.status_code,'error':response.text[:300]}
                    else:
                        project_id=response.json()['project']['id']
                        response=client.post('/api/projects/'+project_id+'/documents',
                              files={'file':(path.name,data,'application/octet-stream')})
                        item['api']={'upload_http_status':response.status_code,
                                     'status':'accepted' if response.status_code in (200,202) else 'rejected',
                                     'error':response.text[:500] if response.status_code>=400 else None}
            except Exception as exc:item['api']={'status':'exception','error':str(exc)[:500]}
        results.append(item)
        (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
        print(sample['id'],item['parse'].get('status'),item['api'].get('upload_http_status'),flush=True)
    brain.query_llm=original_llm
    unchanged=all(hashlib.sha256((source_root/p).read_bytes()).hexdigest()==h for p,h in source_hashes.items())
    report={'schema':'ontoflow.external-audit/v1','ran_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
      'application_version':__import__('app.version',fromlist=['VERSION']).VERSION,
      'source_unchanged':unchanged,'source_file_count':len(source_hashes),
      'python':platform.python_version(),'platform':platform.platform(),
      'total_seconds':round(time.perf_counter()-started,3),'samples':len(results),
      'collection_failures':manifest['collection_failures'],
      'parse_statuses':dict(Counter(r['parse']['status'] for r in results)),
      'api_statuses':dict(Counter(str(r['api'].get('upload_http_status','not_run')) for r in results)),
      'unexpected_model_requests':forbidden[0],
      'limitations':['No real LLM semantic accuracy test','No real PostgreSQL','No OpenMind HTTP write',
        'Public repository fixtures include synthetic upstream cases; collection is new, not a claim of unknown-world accuracy.',
        'Source retention is lexical only; layout/image/OCR/interpretation limitations are separate.']}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (out/'source-hashes.json').write_text(json.dumps(source_hashes,indent=2)+'\n')
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
