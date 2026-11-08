"""Synthetic benchmark for deterministic Markdown extraction (NOT LLM benchmark).

Run from the project root: python -m scripts.bench_discovery --rows 3000
The numbers are local and do not represent production LLM throughput.
"""
import argparse
import time
from app.structured_discovery import markdown_chunks,deterministic_discover


def benchmark(rows):
    lines=['# 信息分发本体（合成基准）','## 3 实体清单',
           '| 实体编号 | 实体名称 | 描述 |','|---|---|---|']
    lines.extend(f'| E-{i:05d} | 实体{i} | 独立业务概念编号 {i} |' for i in range(rows))
    text='\n'.join(lines)
    begin=time.perf_counter()
    chunks=markdown_chunks(text)
    parse_ms=(time.perf_counter()-begin)*1000
    begin=time.perf_counter()
    clues=[deterministic_discover({'content':c['text'],**c}) for c in chunks if c['kind']!='heading']
    discover_ms=(time.perf_counter()-begin)*1000
    extracted=sum(len(c['entities']) for c in clues)
    model_needed=sum(bool(c['_semantic_text']) for c in clues)
    print({'bytes':len(text.encode()),'entity_rows':rows,'source_chunks':len(chunks),
           'extracted_entities':extracted,'semantic_units_requiring_llm':model_needed,
           'parse_ms':round(parse_ms,1),'rules_ms':round(discover_ms,1)})
    assert extracted==rows and model_needed==0


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,default=3000)
    opts=parser.parse_args()
    benchmark(opts.rows)
