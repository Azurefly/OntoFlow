"""Non-destructive HTTP concurrency probe. No document writes.

Run against a separate staging deployment, with the real API token in env.
Example: python -m scripts.bench_load --url http://127.0.0.1:8000 --concurrency 50
"""
import argparse
import asyncio
import os
import time
import statistics
import httpx

async def main(args):
    headers={'X-API-Key':os.getenv('API_TOKEN','')} if os.getenv('API_TOKEN') else {}
    semaphore=asyncio.Semaphore(args.concurrency)
    successes=[];errors=[]
    async with httpx.AsyncClient(base_url=args.url,timeout=15,headers=headers) as client:
        async def probe(i):
            async with semaphore:
                started=time.perf_counter()
                try:
                    result=await client.get(args.path)
                    elapsed=(time.perf_counter()-started)*1000
                    (successes if result.status_code==200 else errors).append(elapsed)
                except httpx.HTTPError:
                    errors.append((time.perf_counter()-started)*1000)
        started=time.perf_counter()
        await asyncio.gather(*(probe(i) for i in range(args.requests)))
        wall=time.perf_counter()-started
        p95=sorted(successes)[min(len(successes)-1,int(.95*len(successes)))] if successes else None
        print({'requests':args.requests,'concurrency':args.concurrency,'success':len(successes),
               'errors':len(errors),'throughput_req_s':round(args.requests/wall,1),
               'p50_ms':round(statistics.median(successes),1) if successes else None,
               'p95_ms':round(p95,1) if p95 is not None else None})

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    parser.add_argument('--path',default='/api/ready')
    parser.add_argument('--concurrency',type=int,default=50)
    parser.add_argument('--requests',type=int,default=500)
    args=parser.parse_args()
    if not 1<=args.concurrency<=500 or not 1<=args.requests<=100000:parser.error('invalid concurrency/requests')
    asyncio.run(main(args))
