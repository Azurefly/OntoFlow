"""Freeze original public files; verify engineering repairs, not semantic accuracy.

Accepts extracted CI artifacts from the prior business and external-format runs.
Never changes bytes, chooses alternate answers or calls a model. Emits all
failures before exiting nonzero. Use --inputs pointing to the combined artifacts.
"""
from __future__ import annotations
import argparse,hashlib,json,os,platform,sys,tempfile,time
from pathlib import Path
from collections import Counter


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();root=Path(args.inputs);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    for key in list(os.environ):
        if key.startswith(('LLM_','OPENMIND_','MODEL_')) or key=='DATABASE_URL':os.environ.pop(key,None)
    os.environ.update(ASYNC_PARSE='false',ONTOFLOW_DEMO_MODE='true')
    from app import brain,incremental
    from app.extraction import extract
    from app.main import create_app
    from app.source_understanding import read_source
    from app.version import VERSION
    from fastapi.testclient import TestClient
    attempts=[0]
    def forbidden(*a,**k):attempts[0]+=1;raise AssertionError('Unexpected real/model mock request')
    brain.query_llm=forbidden
    records=[];failures=[];seen=set()
    for manifest in root.rglob('manifest.json'):
        data=json.loads(manifest.read_text())
        for sample in data.get('samples',[]):
            # Business samples have a file key; old public fixture manifest uses local_path.
            name=sample.get('file') or sample.get('local_path') or ''
            if not name:continue
            paths=list(manifest.parent.rglob(Path(name).name))
            if not paths:continue
            path=next((x for x in paths if x.is_file()),None)
            if path is None:continue
            expected=sample.get('sha256')
            if not expected:continue
            actual=hashlib.sha256(path.read_bytes()).hexdigest()
            if actual!=expected:failures.append({'id':sample['id'],'reason':'source_sha_mismatch'});continue
            if (sample['id'],actual) in seen:continue
            seen.add((sample['id'],actual))
            rec={'id':sample['id'],'file':path.name,'sha256':actual,'bytes':path.stat().st_size,'source_url':sample.get('url') or sample.get('source_url')}
            start=time.perf_counter()
            try:
                parsed=extract(path);rec.update(read_status=parsed['reading_status'],characters=len(parsed['content']),units=len(parsed['chunks']),warnings=parsed['warnings'],parse_seconds=round(time.perf_counter()-start,4))
                doc={'id':'doc','project_id':'project','filename':path.name,'chunks':parsed['chunks']}
                parts=list(incremental.segments([doc]));ids=[x['id'] for x in parts]
                assert len(ids)==len(set(ids)),'duplicate source occurrence ids'
                coverage=Counter();clues={k:[] for k in ('entities','attributes','relations')}
                member_count=0;sem_required=0
                with brain.using_model({'base_url':'','model':'','api_key':''}):
                    for item in parts:
                        member_count+=len(item.get('source_spans',[])) or 1
                        source=read_source(item);needs=bool(source['definitions'].get('_semantic_text','').strip())
                        result=brain.joint_extract_segment(item)
                        sem_required+=needs;coverage[result.get('_state','native')]+=1
                        if needs:assert not result['_complete'],'unmodeled prose incorrectly marked complete'
                        for kind in clues:clues[kind].extend(result[kind])
                        for s in item.get('source_spans',[]):
                            assert 0<=s['start']<s['end']<=len(item['content'])
                rec.update(source_occurrences=member_count,model_units=sem_required,interpretation_units=len(parts),states=dict(coverage),rule_counts={k:len(v) for k,v in clues.items()},entity_names=[v['name'] for v in clues['entities']])
                # Corpus-specific assertions here are regression facts, not model input.
                if sample['id']=='egov_guide':assert not any(e['name']=='报警事件' and '警方' in e.get('description','') for e in clues['entities'])
                if sample['id']=='property_registration':assert len(parts)<200,'short-paragraph call amplification returned'
                if sample['id']=='docling_complex_pdf-1':
                    assert 'Ada Lovelace' in parsed['content'] and 'agree_terms' in parsed['content'],'stored form values lost'
                if sample['id'] in ('docling_docx-2','docling_docx-3'):assert 'Some text' in parsed['content']
            except Exception as exc:
                rec.update(read_status='rejected',error=f'{type(exc).__name__}: {exc}')
                if sample['id'] not in ('docling_legacy_doc-1','pdfminer_pdf-2'):
                    failures.append({'id':sample['id'],'reason':str(exc)})
            try:
                with tempfile.TemporaryDirectory() as directory,TestClient(create_app(Path(directory)),raise_server_exceptions=False) as client:
                    pid=client.post('/api/projects',json={'name':'公开资料修复复验'}).json()['project']['id']
                    res=client.post(f'/api/projects/{pid}/documents',files={'file':(path.name,path.read_bytes(),'application/octet-stream')})
                    rec['http_status']=res.status_code
                    if rec['read_status']!='rejected':assert res.status_code==200,res.text[:500]
                    else:assert res.status_code in (415,422),res.text[:500]
            except Exception as exc:failures.append({'id':sample['id'],'reason':'api: '+str(exc)})
            assert hashlib.sha256(path.read_bytes()).hexdigest()==actual,'source mutated'
            records.append(rec);(out/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(sample['id'],rec['read_status'],rec.get('http_status'),rec.get('interpretation_units'),flush=True)
    if not records:failures.append({'reason':'No verified input bytes found; not an empty successful test'})
    summary={'version':VERSION,'files':len(records),'parse_statuses':dict(Counter(r['read_status'] for r in records)),
             'http_statuses':dict(Counter(str(r.get('http_status')) for r in records)),
             'real_llm_requests':attempts[0],'database':'temporary SQLite','failures':failures,
             'note':'Reading, lossless evidence, routing and upload regression only; not semantic precision or recall.',
             'python':platform.python_version()}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if failures or attempts[0]:sys.exit(1)


if __name__=='__main__':main()
