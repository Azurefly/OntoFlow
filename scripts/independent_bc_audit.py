#!/usr/bin/env python3
"""External no-answer-key audit for OntoFlow schema extraction.

Frozen production code is *only* imported as a subject under test.
Independent ground truth is reconstructed from raw markdown with this script's
own table scanner. There are no expected BC numeric counts in assertions.
No network, OpenMind, DB writes, or model API are used.
"""
import argparse
import collections
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path

APP=Path(os.environ.get('ONTOFLOW_AUDIT_APP', str(Path.cwd()))).resolve()
if str(APP) not in sys.path:sys.path.insert(0,str(APP))
from app.extraction import extract
from app.structured_discovery import deterministic_discover
from app.readable_overview import structured_overview
from app.schema_catalog import build_attribute_catalog
from app.openmind import make_plan
from app.ontology_abstraction import normalize_clues
from app import incremental,brain


def cells(line):
    # Independent from app.structured_discovery._table_cells
    line=line.strip();line=line[1:] if line.startswith('|') else line
    line=line[:-1] if line.endswith('|') else line
    return [x.strip().replace('\\|','|') for x in re.split(r'(?<!\\)\|',line)]


def raw_tables(text):
    """Read Markdown with an independent state machine, no app parser or counts."""
    rows=text.splitlines()
    headings={};i=0
    while i<len(rows):
        h=re.match(r'^\s*(#{1,6})\s+(.*?)\s*$',rows[i])
        if h:
            d=len(h.group(1));headings={k:v for k,v in headings.items() if k<d}
            headings[d]=h.group(2).strip()
            i+=1;continue
        if rows[i].strip().startswith('|') and i+1<len(rows):
            sep=rows[i+1].strip()
            sep_cells=cells(sep)
            if sep.startswith('|') and len(sep_cells)==len(cells(rows[i])) and len(sep_cells)>=2 and all(re.fullmatch(r':?-{3,}:?',x) for x in sep_cells):
                header=cells(rows[i]);records=[];j=i+2
                while j<len(rows) and rows[j].strip().startswith('|'):
                    data=cells(rows[j]);records.append((j+1,{key:data[pos] if pos<len(data) else '' for pos,key in enumerate(header)},rows[j]))
                    j+=1
                yield {'head':header,'records':records,'context':headings.copy(), 'line':i+1}
                i=j;continue
        i+=1


def raw_reference(text):
    entities={}; shared={}; attaches={}; rels={}; warnings=[]; duplicates=collections.Counter()
    for table in raw_tables(text):
        head=set(table['head']);context=table['context']
        top=context.get(2,'');sub=context.get(3,'')
        if {'实体名称','实体编号','实体级别','英文标识'}<=head:
            for line,row,quote in table['records']:
                code=row['实体编号'];name=row['实体名称'];key=(code,name)
                duplicates[('entity',key)]+=1
                if code in entities and entities[code]['name']!=name:warnings.append(f'实体代码重复: {code}')
                entities[code]={'code':code,'name':name,'level':row.get('实体级别'), 'parent':row.get('父实体'),'line':line,'quote':quote}
        elif {'属性名称','属性编号','挂载实体数','挂载实体'}<=head:
            for line,row,quote in table['records']:
                code=row['属性编号'];name=row['属性名称'];owners=[x.strip() for x in re.split('[、，,]',row['挂载实体']) if x.strip()]
                duplicates[('shared',code)]+=1
                if code in shared and shared[code]['name']!=name:warnings.append(f'属性代码重复: {code}')
                shared[code]={'code':code,'name':name,'type':row.get('数据类型',''),
                              'owners':owners,'claimed_count':row['挂载实体数'],'line':line,'quote':quote}
        elif {'属性编号','属性名称','属性角色','是否必填'}<=head and sub:
            # Entity name inferred from nested heading shape, independent of
            # the production regex and from row labels: "6.1 X（xx）— N项".
            m=re.match(r'^\d+(?:\.\d+)*\s+(.+?)(?:\s*[（(][^）)]+[）)])?\s*[—–-]\s*\d+\s*项\s*$',sub)
            if m:
                owner=m.group(1).strip()
                for line,row,quote in table['records']:
                    code=row['属性编号'];name=row['属性名称'];key=(code,owner)
                    duplicates[('attachment',key)]+=1
                    attaches[key]={'code':code,'owner':owner,'name':name,'type':row.get('数据类型',''),
                                   'line':line,'quote':quote}
            else:
                warnings.append(f'无法识别属性归属标题: {sub}')
        elif {'项','内容'}<=head and re.search(r'\bREL[-_]?\d+\b',sub,re.I):
            m=re.search(r'\b(REL[-_]?\d+)\b',sub,re.I)
            key=m.group(1).upper().replace('_','-')
            kv={}; quotes={}
            for line,row,quote in table['records']:
                kv[row['项']]=row['内容'];quotes[row['项']]=quote
            if all(k in kv for k in ('实体A','实体B','关系')):
                rels[key]={'source':kv['实体A'],'target':kv['实体B'],'label':kv['关系'],
                           'cardinality':kv.get('基数'),'line':table['line'],
                           'quote':quotes['关系']}
            else:warnings.append(f'关系结构缺字段 {key}')
    return {'entities':entities,'shared':shared,'attachments':attaches,'relations':rels,
            'duplicates':{str(k):v for k,v in duplicates.items() if v>1},'warnings':warnings}


def production_extract(text):
    # Test subject uses actual public extract() to avoid creating custom chunks
    # that artificially make the app look successful.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'unseen-input.md'
        p.write_text(text,encoding='utf-8')
        chunks=extract(p)['chunks']
    evid_entity=[];evid_attrs=[];evid_rels=[];gaps=[]; counts=collections.Counter()
    for c in chunks:
        if c.get('kind')=='heading':continue
        out=deterministic_discover({'content':c['text'],'section':c.get('section',''),
                                    'kind':c.get('kind','')})
        # The extractor's model-output normalization is also subject under test.
        norm=normalize_clues(out,c['text'])
        # In actual app, it runs this normalization in joint_extract_segment.
        for key, dest in [('entities',evid_entity),('attributes',evid_attrs),('relations',evid_rels)]:
            for x in norm[key]:
                dest.append(dict(x,source_section=c.get('section',''),source_locator=c.get('locator','')))
        gaps.extend(norm['_unresolved'])  # same checkpoint semantics as production; do not double-count
        if out.get('_semantic_text','').strip():counts['semantic_units']+=1
    return {'entities':evid_entity,'attributes':evid_attrs,'relations':evid_rels,
            'gaps':gaps,'counts':dict(counts),'chunks':chunks}


def check(original, reference, actual):
    expected_e={(c,v['name']) for c,v in reference['entities'].items()}
    actual_e={(v.get('code'),v.get('name')) for v in actual['entities']}
    expected_a={(v['code'],v['owner'],v['name']) for v in reference['attachments'].values()}
    actual_a={(v.get('code'),v.get('owner'),v.get('name')) for v in actual['attributes']}
    expected_r={(v['source'],v['target'],v['label']) for v in reference['relations'].values()}
    actual_r={(v.get('source'),v.get('target'),v.get('label')) for v in actual['relations']}
    # Separate code-name equivalence from source/target direction, unlike a
    # superficial "48 rows" count check.
    bad_quotes=[(typ,v.get('source_locator',''),v.get('name') or v.get('predicate'))
                for typ in ('entities','attributes','relations') for v in actual[typ]
                if not v.get('quote') or str(v['quote']) not in original]
    actual_by_code=collections.defaultdict(list)
    for a in actual['attributes']:
        actual_by_code[a.get('code')].append(a)
    shared_check={}
    for code,item in reference['shared'].items():
        expected_owners=set(item['owners'])
        observed_owners={r.get('owner') for r in actual_by_code[code]}
        shared_check[code]={
            'expected_owners':sorted(expected_owners),
            'observed_owners':sorted(observed_owners),
            'source_declared_count':item.get('claimed_count'),
            'matched':expected_owners==observed_owners,
            'type_match':all(r.get('datatype')==_map_type(item['type']) for r in actual_by_code[code])
        }
    # Compare values, not only counts; preview examples only, raw mismatches
    # remain exported for inspection.
    def comparison(expected, observed):
        missing=sorted(expected-observed);extra=sorted(observed-expected)
        return {'gold_rows':len(expected),'output_rows':len(observed),
                'missing_count':len(missing),'extra_count':len(extra),
                'missing_examples':missing[:8],'extra_examples':extra[:8],
                'exact_set_match':not missing and not extra}
    return {
        'entities':comparison(expected_e,actual_e),
        'per_entity_attributes':comparison(expected_a,actual_a),
        'relations_source_target_label':comparison(expected_r,actual_r),
        'evidence_bad_quotes':bad_quotes[:15],
        'shared_attribute_owner_differences':{c:v for c,v in shared_check.items() if not v['matched']},
        'shared_attribute_type_differences':{c:v for c,v in shared_check.items() if not v['type_match']},
        'shared_type_count':len(reference['shared']),
        'shared_owner_match_count':sum(x['matched'] for x in shared_check.values()),
        'raw_duplicate_definitions':reference['duplicates'],
        'raw_warnings':reference['warnings'],
    }


def _map_type(raw):
    mapping={
        '字符串':'string','枚举':'string','布尔':'boolean','整数':'integer','日期':'date','日期时间':'date',
        '小数':'double','浮点':'double','浮点数':'double','数值':'double','文本':'string', 'JSON':'JSON',
    }
    return mapping.get(raw,raw)


def renumber_headings(text):
    # Change all chapter and subsection numbers without changing the content.
    # Numbering is presentation, not semantic identity, so schema should stay.
    return re.sub(r'(?m)^(#{2,6}\s+)(\d+)(?=(?:\.\d+)*\.?\s)',
                  lambda m: m.group(1)+str(int(m.group(2))+20),text)


def swap_catalog_columns(text, seed=0):
    """Permute columns in any tabular definition, retaining header labels."""
    # Original tables only; independently shuffle every table that has
    # Entity/Attribute names, at the same time preserving every row.
    lines=text.splitlines();out=[];i=0;swaps=0
    while i<len(lines):
        if lines[i].strip().startswith('|') and i+1<len(lines) and lines[i+1].strip().startswith('|'):
            h=cells(lines[i]);d=cells(lines[i+1])
            if len(h)==len(d) and h and all(re.fullmatch(r':?-{3,}:?',x) for x in d) and (
                ('实体名称' in h and '实体编号' in h) or ('属性名称' in h and '属性编号' in h)):
                perm=list(range(len(h)))
                random.Random(len(h)*37+swaps+seed*100003).shuffle(perm)
                # Reorder all rows in this table, keeping original cells unmodified.
                j=i
                while j<len(lines) and lines[j].strip().startswith('|'):
                    row=cells(lines[j]);row=row+['']*max(0,len(h)-len(row))
                    out.append('| '+' | '.join(row[k] for k in perm)+' |')
                    j+=1
                i=j;swaps+=1;continue
        out.append(lines[i]);i+=1
    return '\n'.join(out),swaps


def append_valid_entity(text):
    # Change document content, not just headings: valid new row in existing
    # class definition table is not present in any past hardcoded answer.
    lines=text.splitlines();done=False
    for i in range(len(lines)-1):
        if '实体名称' in lines[i] and '英文标识' in lines[i] and lines[i].strip().startswith('|'):
            header=cells(lines[i])
            row={k:'—' for k in header}
            row.update({'序号':'9999','实体名称':'匿名新概念类型','实体编号':'cxnxdlx',
                        '实体级别':'扩展实体','实体类型':'测试类','描述':'用于结构泛化测试的新增概念类型','英文标识':'NovelDomainConcept'})
            new='| '+' | '.join(row[k] for k in header)+' |'
            j=i+2
            while j<len(lines) and lines[j].strip().startswith('|'):j+=1
            lines.insert(j,new);done=True;break
    if not done:raise RuntimeError('No entity definition table found')
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--file',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    src=Path(args.file);raw=src.read_text(encoding='utf-8');frozen={}
    for p in (APP/'app/structured_discovery.py',APP/'app/incremental.py',APP/'app/schema_catalog.py',APP/'app/openmind.py',APP/'app/brain.py'):
        frozen[str(p.relative_to(APP))]=hashlib.sha256(p.read_bytes()).hexdigest()
    summary={'source':src.name,'sha256':hashlib.sha256(src.read_bytes()).hexdigest(),
             'source_bytes':src.stat().st_size,'frozen_code_hashes':frozen,
             'method':'independent markdown oracle + unchanged production extractor',
             'cases':{},'limitations':['Same BC document had already influenced v1.3.4 source; cannot claim truly unseen blind validation.']}
    variants={'original':raw,'renumbered_headings':renumber_headings(raw),
              'new_entity_row':append_valid_entity(raw)}
    reordered,swaps=swap_catalog_columns(raw)
    variants['permuted_columns']=reordered
    summary['metadata']={'shuffled_tables':swaps}
    for name,text in variants.items():
        timer=time.perf_counter();ref=raw_reference(text);oracle_sec=time.perf_counter()-timer
        timer=time.perf_counter();actual=production_extract(text);actual_sec=time.perf_counter()-timer
        validation=check(text,ref,actual)
        summary['cases'][name]={'reference_counts':{
               'entities':len(ref['entities']),'shared_attributes':len(ref['shared']),
               'direct_attribute_bindings':len(ref['attachments']), 'relations':len(ref['relations'])},
           'actual_counts':{'entity_records':len(actual['entities']),
               'attribute_bindings':len(actual['attributes']),'relation_records':len(actual['relations']),
               'unresolved_items':len(actual['gaps']),'semantic_units':actual['counts'].get('semantic_units',0)},
           'comparison':validation,
           'times_sec':{'oracle':round(oracle_sec,5),'production':round(actual_sec,5)}}
    # Full production native output plan for raw original only. No pre-filled
    # expected counts, no manual remediation, no upstream DB or server writes.
    actual=production_extract(raw)
    name2id={e['name']:str(uuid.uuid5(uuid.NAMESPACE_URL,'independent-audit-entity:'+e['name'])) for e in actual['entities']}
    entities=[{'id':eid,'name':n,'kind':'class','description':''} for n,eid in name2id.items()]
    attrs=[];rts=[]
    for attr in actual['attributes']:
        owner_id=name2id.get(attr['owner'])
        if not owner_id:continue
        attrs.append({'id':str(uuid.uuid4()),'owner_id':owner_id,'name':attr['name'],'datatype':attr['datatype'],
                      'required':False,'multivalued':False,'description':attr.get('description',''),
                      'evidence':json.dumps([{'document_id':'blind-audit','quote':attr['quote'],'locator':attr['source_locator'],'verified':True}],ensure_ascii=False)})
    for rel in actual['relations']:
        if rel['source'] in name2id and rel['target'] in name2id:
            rts.append({'id':str(uuid.uuid4()),'source_id':name2id[rel['source']], 'target_id':name2id[rel['target']],
                        'predicate':rel['predicate'],'description':rel.get('description','')})
    snap={'project':{'revision':1},'entities':entities,'attributes':attrs,'relations':rts}
    domain=str(uuid.uuid4());branch=str(uuid.uuid4())
    native=make_plan(snap,'audit',domain,branch)
    summary['native_plan_original']={'ready':native['ready'],'counts':native['counts'],
                'blocker_count':len(native['blockers']),'blocker_examples':native['blockers'][:30],
                'warning_count':len(native['warnings'])}
    # Coverage of native candidate approval must be distinguished from extraction.
    summary['limitations']+=['Without real human approval, the native payload is only a simulated preview.',
             'No OpenMind service, real PostgreSQL, or external LLM was called.',
             'No key-preserving partial change request is written to the real branch.']
    Path(args.output).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,case in summary['cases'].items():
        c=case['comparison']
        print(name,case['reference_counts'],case['actual_counts'],
              {'matched':[c[k]['exact_set_match'] for k in ('entities','per_entity_attributes','relations_source_target_label')],
               'missing':[c[k]['missing_count'] for k in ('entities','per_entity_attributes','relations_source_target_label')],
               'extra':[c[k]['extra_count'] for k in ('entities','per_entity_attributes','relations_source_target_label')]})
    print('Native plan',summary['native_plan_original'])
    print('Saved:',args.output)

if __name__=='__main__':main()
