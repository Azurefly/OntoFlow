"""Repeatable JSON/ZIP extraction verification, with no LLM or OpenMind HTTP calls.

Run: python -m scripts.bench_json_extraction --file /path/to/export.zip
Outputs only aggregate statistics and no raw source records.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

from app import brain,incremental
from app.extraction import extract
from app.openmind_baseline import canonical_catalog


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--file',required=True)
    opt=parser.parse_args();path=Path(opt.file)
    before=perf_counter();parsed=extract(path)
    parse_time=perf_counter()-before
    document={'id':'local','project_id':'local','filename':path.name,
              'chunks':json.dumps(parsed['chunks'],ensure_ascii=False)}
    source_catalog=None
    if path.suffix.lower()=='.zip':
        with ZipFile(path) as z:source_catalog=canonical_catalog(json.loads(z.read('data.json')))
    elif path.suffix.lower()=='.json':
        raw=json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(raw,dict) and all(k in raw for k in ('entities','attributes','relations')):
            source_catalog=canonical_catalog(raw)
    started=perf_counter()
    summary=brain.analyze([document],baseline=source_catalog)
    counts=Counter();gaps=[];reuse=Counter()
    before_model=brain.query_llm
    def forbid(*a,**kwargs):raise RuntimeError('Unexpected LLM call in schema JSON extraction')
    brain.query_llm=forbid
    try:
        for piece in incremental.segments([document]):
            extracted=brain.joint_extract_segment(piece)
            counts['segments']+=1
            counts['model_calls']+=extracted['_coverage'].get('model_calls',0)
            for k in ('entities','attributes','relations'):
                counts[k]+=len(extracted[k])
            gaps.extend(extracted['_unresolved'])
            if source_catalog is not None:
                for stage in ('ENTITY','PROPERTY','RELATION'):
                    items,issues=incremental.align_clues(stage,extracted,
                          {'entities':[],'attributes':[],'relations':[]},piece,baseline=source_catalog)
                    reuse[stage]+=len(items)
                    gaps.extend(issues)
    finally:brain.query_llm=before_model
    elapsed=perf_counter()-started
    output={'source_filename':path.name,'source_size_bytes':path.stat().st_size,
           'format':summary['coverage']['source_formats'],
           'source_schema_counts':summary['coverage'].get('schema_definitions'),
           'typed_chunk_kinds':dict(Counter(x['kind'] for x in parsed['chunks'])),
           'extracted_type_clues':dict(counts),
           'unresolved_questions':len(gaps),
           'same_branch_new_proposals':dict(reuse) if source_catalog is not None else 'not_checked',
           'summary_chars':len(summary['summary']),
           'timings_seconds':{'parse':round(parse_time,4),'overview_and_extraction':round(elapsed,4)},
           'scope':'离线结构/语义提取与同分支对齐；未调用真实数据库或 OpenMind 服务。'}
    print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
