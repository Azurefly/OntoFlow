"""Run the actual application on frozen public business excerpts via a REAL server.

No mocked model response or ontology answer is embedded. Extracted candidates are
validated and previewed locally, not approved in a user's workspace or OpenMind.
The automated gate checks grounding/structure, NOT full-domain semantic recall.
"""
from __future__ import annotations
import argparse,hashlib,json,os,time,traceback,uuid
from pathlib import Path
from urllib.parse import urlsplit


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--inputs',required=True);p.add_argument('--output',required=True)
    p.add_argument('--base-url',default='http://127.0.0.1:8766/v1')
    p.add_argument('--model',default='Qwen2.5-7B-Instruct-Q4_K_M')
    p.add_argument('--structured-output',choices=['off','json_object','json_schema'],default='json_schema')
    p.add_argument('--attempts',type=int,choices=range(1,4),default=2)
    p.add_argument('--request-timeout',type=int,default=180)
    p.add_argument('--max-tokens',type=int,default=3072)
    args=p.parse_args();root=Path(args.inputs);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    if urlsplit(args.base_url).hostname not in ('localhost','127.0.0.1'):
        raise SystemExit('This credential-free audit uses a loopback model server only.')
    os.environ.update(ONTOFLOW_DEMO_MODE='true',LLM_REQUEST_TIMEOUT_SECONDS=str(args.request_timeout),
        LLM_SEMANTIC_MAX_TOKENS=str(args.max_tokens),
        JOINT_EXTRACT_CALL_BUDGET='1',JOINT_MAX_RETRY_ROUNDS='3',NO_PROXY='127.0.0.1,localhost')
    import httpx
    from app import brain,incremental
    from app.extraction import extract
    from scripts.semantic_preview import validate_for_review
    from app.naming import prepare_payload,code_error
    from app.version import VERSION
    from app.semantic_contract import locate_quote
    specs=[
        ('property_registration.html','不动产登记机构应当依法确定',650,'不动产登记受理业务类型'),
        ('beijing_rail.pdf','事发轨道交通运营企业核实',650,'轨道交通应急信息报送业务类型'),
        ('hospital_outpatient_service.pdf','三、岗位职责',550,'门诊导诊服务业务类型，不纳入费用支付'),
    ]
    inputs=[]
    for filename,anchor,budget,goal in specs:
        path=next(root.rglob(filename));parsed=extract(path)
        item=next(c for c in parsed['chunks'] if anchor in c['text'])
        full=item['text'];at=full.index(anchor);fragment=full[at:at+budget]
        boundary=max(fragment.rfind('。'),fragment.rfind('\n\n'))
        if boundary>100:fragment=fragment[:boundary+1]
        spans=[]
        for span in item.get('source_spans',[]):
            lo=max(at,span['start']);hi=min(at+len(fragment),span['end'])
            if hi>lo:spans.append({**span,'start':lo-at,'end':hi-at})
        inputs.append({'file':filename,'text':fragment,'goal':goal,'locator':item['locator'],
            'source_spans':spans,'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'excerpt_sha256':hashlib.sha256(fragment.encode()).hexdigest()})
    (out/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2))
    requests=[]
    def on_request(request):
        if len(requests)>=len(inputs)*args.attempts:raise RuntimeError('Finite model audit request budget exhausted')
        if urlsplit(str(request.url)).hostname!='127.0.0.1':raise RuntimeError('Unexpected external host')
        request.extensions['start']=time.perf_counter()
        request.extensions['audit_index']=len(requests)
        requests.append({'request_index':len(requests)+1,'status':None,'state':'started',
            'request':json.loads(request.content)})
        (out/'actual-requests.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2))
    def on_response(response):
        response.read();data={}
        try:data=response.json()
        except ValueError:pass
        request=response.request
        requests[request.extensions['audit_index']].update(seconds=round(time.perf_counter()-request.extensions['start'],3),
            status=response.status_code,state='responded',response=data if data else response.text[:3000])
        (out/'actual-requests.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2))
    # Real HTTP transport. No monkeypatch of query_llm or injected response values.
    brain._HTTP_CLIENT=httpx.Client(timeout=httpx.Timeout(args.request_timeout,connect=15),trust_env=False,
        event_hooks={'request':[on_request],'response':[on_response]})
    results=[]
    (out/'results.json').write_text('[]\n')
    for i,source in enumerate(inputs):
        piece={'document_id':f'public-{i}','project_id':'semantic-audit','content':source['text'],
            'content_hash':source['excerpt_sha256'],'locator':source['locator'],
            'kind':'prose','section':'业务正文','source_spans':source['source_spans']}
        config={'base_url':args.base_url,'api_key':'','model':args.model,
            'structured_output':args.structured_output,
            'interpretation':{'goal':source['goal'],'reading_mode':'semantic_review'}}
        started=time.perf_counter();before=len(requests);history=[];result={'file':source['file'],'goal':source['goal']}
        try:
            clue=None
            def checkpoint(value):
                (out/f'case-{i+1}-checkpoint.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
            for attempt in range(args.attempts):
                with brain.using_model(config):clue=brain.joint_extract_segment(piece,checkpoint=clue,on_checkpoint=checkpoint)
                history.append(clue)
                if clue.get('_complete') or clue.get('_state') not in ('retryable',):break
            result['clues']=clue;result['attempts']=len(history)
            (out/f'case-{i+1}-history.json').write_text(json.dumps(history,ensure_ascii=False,indent=2))
            current={'entities':[],'attributes':[],'relations':[]};validation=[]
            # This is a dry-run dependency-order preview. It is NOT an approval
            # action and does not mutate local/remote business state.
            for stage,key in [('ENTITY','entities'),('PROPERTY','attributes'),('RELATION','relations')]:
                proposals,gaps=incremental.align_clues(stage,clue,current,piece)
                validation.extend({'stage':stage,'kind':'alignment','detail':x['reason']} for x in gaps)
                for candidate in proposals:
                    try:
                        obj=validate_for_review(candidate['kind'],candidate['payload'],candidate['evidence'],current)
                        obj['id']=str(uuid.uuid4());obj['evidence']=candidate['evidence'];current[key].append(obj)
                    except Exception as exc:validation.append({'stage':stage,'kind':'invalid','detail':str(exc)})
            evidence_valid=all(locate_quote(source['text'],x.get('quote')) is not None
                for key in current for x in clue[key])
            coding_valid=all(not code_error({'entities':'entity','attributes':'attribute','relations':'relation'}[key],x['code']) for key in current for x in current[key])
            result.update(preview=current,validated_counts={k:len(v) for k,v in current.items()},validation=validation,
                checks={'grounded_quotes':evidence_valid,'legal_codes':coding_valid,'no_alignment_errors':not validation,
                        'has_types_and_connections':bool(current['entities'] and (current['attributes'] or current['relations'])),
                        'extraction_contract_complete':bool(clue.get('_complete'))})
            result['contract_gate_passed']=all(result['checks'].values())
        except Exception as exc:
            result.update(error=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc()[-2000:],contract_gate_passed=False)
        result.update(seconds=round(time.perf_counter()-started,3),actual_requests=len(requests)-before)
        results.append(result)
        (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(source['file'],result.get('validated_counts'),result.get('contract_gate_passed'),flush=True)
    summary={'version':VERSION,'model':args.model,'structured_output':args.structured_output,
        'cases':len(results),'real_requests':len(requests),'contract_cases_passed':sum(x['contract_gate_passed'] for x in results),
        'request_errors':sum(x['status']!=200 for x in requests),
        'attempts_per_case_limit':args.attempts,'request_timeout_seconds':args.request_timeout,
        'max_output_tokens_per_request':args.max_tokens,
        'scope':'Three fixed real-source excerpts; contract + evidence + dependency preview, NOT full-document accuracy or OpenMind approval',
        'human_gold_annotation':False,'failures_preserved':True}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    if not all(x['contract_gate_passed'] for x in results):raise SystemExit('Semantic contract gate failed; inspect actual outputs, do not claim full success.')


if __name__=='__main__':main()
