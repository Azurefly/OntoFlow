"""Source evidence cache is independent of modeling intent.

Input formats select readers only. Unknown layouts/examples are never silently
promoted to authoritative ontology definitions; prose remains for interpretation.
"""
from __future__ import annotations
import copy
import json
import re
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from .modeling import neutral_basis, digest
from .store import j,now
from .structured_discovery import deterministic_discover
from .json_discovery import NATIVE_KINDS,discover_json_clues
from .ontology_abstraction import discover_narrative_types,normalize_clues
from .metrics import count

SOURCE_VERSION='source-evidence-v141.1'
_PROVIDER=ContextVar('source_discovery_provider',default=None)


def source_key(piece):
    return digest({'version':SOURCE_VERSION,'content':piece['content_hash'],
        'section':piece.get('section',''),'kind':piece.get('kind',''),
        'context_before':piece.get('context_before',''),
        'source_part':piece.get('source_part',''),'layout_warning':piece.get('layout_warning','')})


@contextmanager
def using_source_cache(store):
    token=_PROVIDER.set(store)
    try:yield
    finally:_PROVIDER.reset(token)


def _discover(piece):
    text=piece['content'];kind=piece.get('kind','')
    if kind in NATIVE_KINDS:
        record_structure=(kind in ('json-class','json-field') and
                          ('业务集合' in piece.get('section','') or '集合字段' in piece.get('section','')))
        found=neutral_basis(discover_json_clues(piece),'inference' if record_structure else 'source_explicit',
                           'record_structure' if record_structure else 'native_schema',
                           '从业务集合或字段结构归纳的类型建议，不是原文明示的本体定义' if record_structure
                           else '来源结构中显式声明的类型定义；不代表目标分支已接受')
        return {'definitions':normalize_clues(found,text),
                'narrative':{'entities':[],'attributes':[],'relations':[],
                            '_unresolved':[],'_coverage':{'narrative_type_clues':0}}}
    if kind in ('ambiguous_table','textbox','note') or piece.get('layout_warning'):
        found={'entities':[],'attributes':[],'relations':[],
            '_semantic_text':text,'_unresolved':[{'category':'source_layout',
            'source_excerpt':text[:160],'reason':'此来源结构或归属不明确，保留原文供语义理解和人工核查；未把列或位置直接当作本体定义',
            'blocks_coverage':False}], '_coverage':{'ambiguous_layout':1}}
    else:
        found=deterministic_discover(piece)
    tentative=bool(re.search(r'示例|样例|假设|拟议|备选|草案|建议方案',piece.get('section','')))
    found=neutral_basis(found,'inference' if tentative else 'source_explicit',
        'tentative_source' if tentative else 'structured_rule',
        '来源明确标为示例/草案/假设，提取不代表正式业务定义' if tentative else '原文明确结构可定位；需人工确认是否纳入本次本体')
    if tentative:
        found['_unresolved'].append({'category':'source_status','source_excerpt':piece.get('section','')[:160],
            'reason':'本段来自示例、假设或草案，请确认其是否作为本次建模依据；不自动当作定稿',
            'blocks_coverage':False})
    narrative=neutral_basis(discover_narrative_types(found.get('_semantic_text','')),
        'inference','rule_abstraction','由具体业务描述归纳的类型建议，不是原文直接声明的本体定义')
    return {'definitions':normalize_clues(found,text),'narrative':normalize_clues(narrative,text)}


def read_source(piece):
    store=_PROVIDER.get()
    if store is None:
        count('source_rule_runs');return _discover(piece)
    key=source_key(piece)
    row=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',(key,))
    if row:
        count('source_cache_hits');return json.loads(row['result'])
    count('source_cache_misses');count('source_rule_runs')
    result=_discover(piece)
    with store.tx() as c:
        c.execute('''INSERT INTO extraction_cache(cache_key,content_hash,model_name,extractor_version,result,created_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING''',
            (key,piece['content_hash'],'',SOURCE_VERSION,j(result),now()))
    return copy.deepcopy(result)


def material_profile(documents):
    """Structural observation, not authoritative semantic classification."""
    report={'documents':[],'total_units':0,'unverified_source_parts':0,
            'basis':'source_observation','notice':'材料特征用于提示，不替代用户的主题、范围和建模决定。'}
    for document in documents:
        chunks=document.get('chunks') or []
        if isinstance(chunks,str):chunks=json.loads(chunks)
        warnings=document.get('warnings') or []
        if isinstance(warnings,str):warnings=json.loads(warnings)
        counts=Counter(chunk.get('kind') or 'unclassified' for chunk in chunks)
        text='\n'.join(chunk.get('text','') for chunk in chunks)
        proposed=bool(re.search(r'建议|拟|可能|假设|示例|草案',text))
        if any(k.startswith('native-') for k in counts):label='包含显式本体结构'
        elif counts.get('table') and counts.get('prose'):label='表格与叙述混合材料'
        elif counts.get('table'):label='以表格为主，业务含义仍需确认'
        else:label='以叙述为主，需语义理解与协商'
        report['documents'].append({'id':document.get('id',''),'filename':document['filename'],
            'observed_profile':label,'units':len(chunks),'kinds':dict(counts),
            'contains_tentative_language':proposed,'warnings':warnings,
            'readers':sorted({str(c.get('parser','legacy')) for c in chunks}),
            'encodings':sorted({str(c['encoding']) for c in chunks if c.get('encoding')}),
            'structurally_complete':not warnings})
        report['total_units']+=len(chunks)
        report['unverified_source_parts']+=len(warnings)
    report['complete']=report['unverified_source_parts']==0
    return report
