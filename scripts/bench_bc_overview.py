"""Offline BC Markdown quality and cost benchmark; no LLM, no network or PG.

The source is supplied by the operator and is not included in the release zip.
"""
from __future__ import annotations
import argparse
import json
import time
from collections import Counter
from pathlib import Path

from app import brain, incremental
from app.extraction import extract
from app.readable_overview import structured_overview
from app.structured_discovery import deterministic_discover


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--file',required=True,help='original BC .md document')
    args=parser.parse_args()
    source=Path(args.file)
    started=time.perf_counter()
    doc=extract(source)
    document={'id':'local-benchmark','project_id':'local-benchmark','filename':source.name,'chunks':json.dumps(doc['chunks'],ensure_ascii=False)}
    parsed_seconds=time.perf_counter()-started
    started=time.perf_counter()
    outline=structured_overview([document],baseline={'entities':[],'attributes':[],'relations':[]})
    if outline is None:raise SystemExit('Source does not meet the structured-ontology benchmark requirements.')
    summaries=outline['coverage']
    # Forbid model calls during discovery. No API Key is needed for this test.
    original=brain.query_llm
    def forbidden(*args,**kwargs):
        raise RuntimeError('Unexpected model request while parsing explicit definitions')
    brain.query_llm=forbidden
    counts=Counter()
    try:
        with brain.using_model({'base_url':'https://invalid.example/v1','model':'offline-test','api_key':''}):
            for piece in incremental.segments([document]):
                out=brain.joint_extract_segment(piece)
                counts['segments']+=1
                counts['unresolved']+=len(out['_unresolved'])
                counts['model_calls']+=out['_coverage'].get('model_calls',0)
                counts['incomplete']+=not out['_complete']
                for key in ('entities','attributes','relations'):
                    counts[key]+=len(out[key])
    finally:
        brain.query_llm=original
    processing_seconds=time.perf_counter()-started
    print(json.dumps({'source_bytes':source.stat().st_size,
        'structured_overview':{k:v for k,v in summaries.items() if k not in ('issues','openmind_baseline')},
        'rule_discovery':dict(counts),
        'timing_seconds':{'document_parse':round(parsed_seconds,4),'rule_extract':round(processing_seconds,4)},
        'note':'Offline parsing/algorithm benchmark, not actual PostgreSQL/LLM throughput. Failed/uncovered JSON properties are not silently treated as complete.'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
