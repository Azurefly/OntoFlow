"""Optional REAL CPU model smoke test on public business excerpts.

No preset answers and no mocked query_llm. Loads official safetensors weights,
serves a loopback chat-completions endpoint and calls the application normally.
This is a small excerpt probe, NOT full-document semantic accuracy or recall.
"""
from __future__ import annotations
import argparse,hashlib,json,os,threading,time,traceback
from pathlib import Path

MODEL_ID='Qwen/Qwen2.5-1.5B-Instruct'


def sha_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def main():
    args=argparse.ArgumentParser();args.add_argument('--inputs',required=True);args.add_argument('--output',required=True)
    ns=args.parse_args();root=Path(ns.inputs);out=Path(ns.output);out.mkdir(parents=True,exist_ok=True)
    os.environ.update(ONTOFLOW_DEMO_MODE='true',LLM_REQUEST_TIMEOUT_SECONDS='600',JOINT_EXTRACT_CALL_BUDGET='1',NO_PROXY='127.0.0.1,localhost')
    from app.extraction import extract
    from app import brain,incremental
    from app.ontology import validate_payload
    # Selection uses source business sections, not the extractor's output counts.
    specs=[
        ('property_registration.html','不动产登记机构应当依法确定',650,'不动产登记受理业务类型'),
        ('beijing_rail.pdf','事发轨道交通运营企业核实',650,'轨道交通应急信息报送业务类型'),
        ('hospital_outpatient_service.pdf','三、岗位职责',550,'门诊导诊服务业务类型，不纳入费用支付'),
    ]
    inputs=[]
    for filename,anchor,budget,goal in specs:
        path=next(root.rglob(filename))
        parsed=extract(path)
        item=next(c for c in parsed['chunks'] if anchor in c['text'])
        text=item['text'];at=text.index(anchor);fragment=text[at:at+budget]
        # End at an existing clause boundary; never supply a rewritten synopsis.
        boundary=max(fragment.rfind('。'),fragment.rfind('\n\n'))
        if boundary>100:fragment=fragment[:boundary+1]
        inputs.append({'file':filename,'source_sha256':sha_file(path),'locator':item['locator'],
                       'text':fragment,'goal':goal,'excerpt_sha256':hashlib.sha256(fragment.encode()).hexdigest()})
    (out/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2))
    import torch,transformers,uvicorn
    from huggingface_hub import HfApi,snapshot_download
    from transformers import AutoTokenizer,AutoModelForCausalLM
    from fastapi import FastAPI,HTTPException
    revision=HfApi().model_info(MODEL_ID).sha
    folder=Path(snapshot_download(MODEL_ID,revision=revision,
        allow_patterns=['*.json','*.safetensors','*.txt','*.model','*.tiktoken'],max_workers=4))
    torch.set_num_threads(min(4,os.cpu_count() or 2));torch.set_num_interop_threads(1)
    tokenizer=AutoTokenizer.from_pretrained(folder,trust_remote_code=False)
    model=AutoModelForCausalLM.from_pretrained(folder,torch_dtype=torch.float32,
        trust_remote_code=False,use_safetensors=True,attn_implementation='sdpa').eval()
    model_files={p.name:sha_file(p) for p in folder.iterdir() if p.is_file()}
    metadata={'model':MODEL_ID,'revision':revision,'weight_files_sha256':model_files,
        'torch':torch.__version__,'transformers':transformers.__version__,'dtype':'float32','device':'cpu',
        'scope':'3 authentic-source excerpts, not whole-document or gold-standard semantic validation'}
    (out/'model.json').write_text(json.dumps(metadata,indent=2))
    app=FastAPI();records=[];lock=threading.Lock()
    @app.post('/v1/chat/completions')
    def complete(body:dict):
        with lock:
            if len(records)>=9:raise HTTPException(429,'Bounded smoke-test request budget exhausted')
            started=time.perf_counter()
            inputs=tokenizer.apply_chat_template(body['messages'],tokenize=True,add_generation_prompt=True,
                return_dict=True,return_tensors='pt')
            if inputs['input_ids'].shape[1]>10000:raise HTTPException(413,'Smoke prompt exceeds token bound')
            max_tokens=min(int(body.get('max_tokens',1536)),1536)
            with torch.inference_mode():
                generated=model.generate(**inputs,max_new_tokens=max_tokens,do_sample=False,
                    pad_token_id=tokenizer.eos_token_id)
            tokens=generated[0,inputs['input_ids'].shape[1]:]
            content=tokenizer.decode(tokens,skip_special_tokens=True)
            eos=model.generation_config.eos_token_id
            eos_ids=[eos] if isinstance(eos,int) else (eos or [tokenizer.eos_token_id])
            finish='length' if len(tokens)>=max_tokens and int(tokens[-1]) not in eos_ids else 'stop'
            usage={'prompt_tokens':int(inputs['input_ids'].shape[1]),'completion_tokens':len(tokens),
                   'total_tokens':int(inputs['input_ids'].shape[1])+len(tokens)}
            records.append({'request_index':len(records)+1,'seconds':round(time.perf_counter()-started,3),
                'messages':body['messages'],'output':content,'finish_reason':finish,'usage':usage})
            (out/'actual-inference.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            return {'choices':[{'message':{'role':'assistant','content':content},'finish_reason':finish}], 'usage':usage}
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8765,log_level='warning'))
    threading.Thread(target=server.run,daemon=True).start()
    for _ in range(100):
        if server.started:break
        time.sleep(.1)
    if not server.started:raise RuntimeError('Loopback inference server did not start')
    results=[]
    for number,source in enumerate(inputs,1):
        piece={'document_id':f'probe-{number}','project_id':'probe','content':source['text'],
            'content_hash':hashlib.sha256(source['text'].encode()).hexdigest(),'kind':'prose','section':'业务正文',
            'locator':source['locator']}
        config={'base_url':'http://127.0.0.1:8765/v1','api_key':'','model':MODEL_ID,
            'interpretation':{'goal':source['goal'],'reading_mode':'semantic_review'}}
        result={'file':source['file'],'goal':source['goal']};started=time.perf_counter();before=len(records)
        try:
            with brain.using_model(config):clues=brain.joint_extract_segment(piece)
            result['clues']=clues
            snapshot={'entities':[],'attributes':[],'relations':[]};validation=[]
            for stage,key in [('ENTITY','entities'),('PROPERTY','attributes'),('RELATION','relations')]:
                proposals,gaps=incremental.align_clues(stage,clues,snapshot,piece)
                validation.extend({'stage':stage,'kind':'alignment_gap','detail':g['reason']} for g in gaps)
                for proposal in proposals:
                    try:
                        obj=validate_payload(proposal['kind'],proposal['payload'],snapshot)
                        obj['id']=str(__import__('uuid').uuid4());snapshot[key].append(obj)
                    except Exception as exc:validation.append({'stage':stage,'kind':'invalid','detail':str(exc)})
            result.update(validated_counts={k:len(v) for k,v in snapshot.items()},validation=validation)
        except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)
        result.update(seconds=round(time.perf_counter()-started,3),actual_requests=len(records)-before)
        results.append(result);(out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(source['file'],result.get('validated_counts'),result.get('error'),flush=True)
    server.should_exit=True
    (out/'summary.json').write_text(json.dumps({**metadata,'cases':len(results),'real_requests':len(records),
        'result_errors':sum('error' in r for r in results),'human_gold_annotation':False,
        'not_proven':['Full-document recall','Larger provider performance','Production OpenMind writes']},indent=2))


if __name__=='__main__':main()
