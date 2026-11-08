"""Readable, evidence-grounded *overview* of structured ontology source documents.

The overview is not an extraction result: it highlights business intent, scope,
reliable document counts, and decision points. Source inventory/coverage is
returned as separate machine-readable metadata, never concatenated into the prose.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from .table_semantics import is_shared_attribute_table, is_entity_attachment_index

VERSION = 'readable-overview-v1'
_TOP = re.compile(r'^#\s+(.+?)\s*$', re.M)
_REL = re.compile(r'\b(REL[-_ ]?\d{1,4})\s*[｜|:：—-]\s*(.+?)\s*[→➜➡]\s*(.+?)\s*[｜|:：—-]\s*(.+?)(?:\s*[（(][^）)]*[）)])?\s*$')


def _cells(text):
    return [s.strip().replace('\\|', '|') for s in re.split(r'(?<!\\)\|', text.strip().strip('|'))]


def _doc_chunks(doc):
    chunks = doc.get('chunks') or []
    if isinstance(chunks, str):
        chunks = json.loads(chunks)
    return chunks


def _overview_source(documents):
    entities = {}
    attrs = {}
    rels = {}
    domains = {}
    sections = set()
    title = ''
    note = ''
    tables_seen = 0
    for doc in documents:
        for chunk in _doc_chunks(doc):
            text = str(chunk.get('text', ''))
            kind = chunk.get('kind', '')
            section = str(chunk.get('section', ''))
            if kind == 'heading':
                line = text.strip().split('\n')[0]
                m = _TOP.match(line)
                if m and not title:
                    title = m.group(1).strip()
                if re.match(r'^#{2,3}\s+', line):
                    sections.add(re.sub(r'^#{2,3}\s+', '', line).strip())
                relation = _REL.search(re.sub(r'^#{1,6}\s*\d+(?:\.\d+)?\s*', '', line).strip())
                if relation:
                    key = relation.group(1).replace(' ', '-').replace('_', '-').upper()
                    rels[key] = {'source': relation.group(2).strip(),
                                 'target': relation.group(3).strip(),
                                 'label': relation.group(4).strip()}
            if '实体初稿清单' in text and ('核心实体14个' in text or '核心实体 14 个' in text):
                note = '文档中“核心实体14个”的旧表述与实体清单需核对。'
            if kind != 'table':
                continue
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) < 3:
                continue
            header = _cells(lines[0])
            if not re.match(r'^\s*\|?\s*:?-{3,}', lines[1]):
                continue
            index = {name: i for i, name in enumerate(header)}
            # Always use the table header and *its section*, never a prose guess.
            if (('实体名称' in index and '实体编号' in index)
                    and not is_entity_attachment_index(section,header)):
                typ = 'entity'
            elif is_shared_attribute_table(section,header):
                typ = 'attribute'
            elif '能力域编号' in index and '能力域' in index:
                typ = 'capability'
            else:
                continue
            tables_seen += 1
            for line in lines[2:]:
                if not line.startswith('|'):
                    continue
                row = _cells(line)
                if len(row) < len(header):
                    continue
                def c(name): return row[index[name]].strip() if name in index else ''
                if typ == 'entity':
                    key = c('实体编号')
                    if key and c('实体名称'):
                        entities[key] = {'name': c('实体名称'), 'level':c('实体级别'),
                                         'category':c('实体类型')}
                elif typ == 'attribute':
                    key = c('属性编号')
                    if key and c('属性名称'):
                        attrs[key] = {'name':c('属性名称'), 'type':c('数据类型'),
                                      'extended':'扩展字段（建议新增）' in line or '待系统落库回填' in line}
                else:
                    key = c('能力域编号').upper()
                    if re.fullmatch(r'D\d{2}', key) and c('能力域'):
                        raw = c('原子能力数')
                        domains[key] = {'name':c('能力域'), 'count':int(raw) if raw.isdigit() else None}
    counts = Counter(e['level'] for e in entities.values())
    return {'title':title, 'entities':entities,'attributes':attrs,'relations':rels,
            'domains':domains,'levels':counts,'sections':sorted(sections),
            'note':note,'tables':tables_seen}


def _pick_names(values, limit=6):
    names = []
    for item in values:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return '、'.join(names)


def json_overview(documents,baseline=None):
    """Present source JSON definitions without turning examples into instances."""
    rows=[]
    for d in documents:
        chunks=_doc_chunks(d)
        for chunk in chunks:
            if chunk.get('kind')=='json-overview':
                rows.append(json.loads(chunk['text']))
    if not rows or len(rows)!=len(documents):return None
    totals=Counter();bindings=0;questions=[];names=[];formats=[]
    for item in rows:
        for key,val in (item.get('counts') or {}).items():
            if isinstance(val,int):totals[key]+=val
        bindings+=int(item.get('directAttributeBindings') or 0)
        names.append(str(item.get('domain') or 'JSON 来源'))
        formats.append(item.get('format'))
        if item.get('unboundAttributes'):
            questions.append(f'有 {item["unboundAttributes"]} 项独立属性没有绑定实体，须人工决定是否保留为独立定义。')
        for field in item.get('unresolved') or []:
            if isinstance(field,dict):
                questions.append(f'{field.get("collection","")}.{field.get("field","")}：{field.get("reason","")}')
    title='、'.join(names[:3])
    if all(k=='openmind-native' for k in formats):
        text='\n'.join([
            f'本资料是「{title}」的 OpenMind 原生本体导出。优先核对当前绑定的 OpenMind 分支，不能将导出内容直接视为新本体或业务实例。',
            '', '一、已有本体结构',
            f'• 实体类型：{totals["entities"]} 个；独立属性定义：{totals["attributes"]} 项；直接属性绑定：{bindings} 条。',
            f'• 原生关系：{totals["relations"]} 条；另有实体继承，通过 parentId 独立核查。',
            f'• 方法：{totals["actions"]} 项，能力：{totals["capabilities"]} 项；保留作参考，不自动转为本体候选。',
            '', '二、建模与对齐边界',
            '• 当前分支已存在的对象优先复用；不同分支的源 UUID 不能直接用作目标 UUID。',
            '• 未绑定属性需要人工决定归属；资源绑定、执行逻辑与实例值不随本体类型候选复制。'])
        scope='以原生 Entity、Attribute、Relation 为定义来源，人工审核后增量构建；对齐并复用现有 OpenMind CODE、UUID。'
    else:
        text='\n'.join([
            f'本资料是「{title}」的记录型 JSON，而非现成的本体定义。',
            '', '一、结构概览',
            f'• 根据集合名称识别 {totals["entities"]} 个实体类型候选及 {totals["attributes"]} 项标量属性定义。',
            '', '二、建模边界',
            '• 仅使用集合和字段定义；姓名、号码、具体记录值不进入本体。',
            '• 嵌套对象、数组及关系方向尚不明确的内容需要人工确定。'])
        scope='以 JSON 集合名称和稳定字段结构作为候选，关系类型与业务角色都需用户确认。'
    return {'summary':text,'scope':scope,'questions':questions[:12],
        'coverage':{'complete':True,'strategy':'json-schema-evidence','source_formats':formats,
                    'schema_definitions':dict(totals),'direct_attribute_bindings':bindings,
                    'issues':questions[:12],
                    'openmind_baseline':({k:len(baseline.get(k,[])) for k in ('entities','attributes','relations')}
                                         if baseline is not None else None)}}


def structured_overview(documents, baseline=None):
    """Return a readable overview when the source includes authoritative tables.

    Free-text narratives and small non-standard documents fall back to semantic
    summarization. Nothing here treats an empty OpenMind branch as new objects.
    """
    info = _overview_source(documents)
    e, a, r, domains = (info[k] for k in ('entities','attributes','relations','domains'))
    if len(e) < 3 or len(r) < 3:
        return None
    title = info['title'] or (documents[0]['filename'].rsplit('.',1)[0] if documents else '业务领域')
    # Avoid concatenating every raw catalog entry: the complete inventory is
    # already available through candidate extraction and the document navigator.
    topic = re.sub(r'\s*[（(][^）)]{0,16}[）)]\s*', '', title)
    topic = re.sub(r'\s*[Vv]\d+(?:\.\d+)*\s*$', '', topic).strip()
    topic = re.sub(r'(?:领域)?本体设计(?:说明|文档)?$', '', topic).strip() or title
    category = Counter(x['category'] for x in e.values() if x['category'])
    category_text = _pick_names((k for k,_ in category.most_common()), 6)
    core, extended = info['levels'].get('核心实体',0), info['levels'].get('扩展实体',0)
    group_desc = f'（核心 {core} 类、扩展 {extended} 类）' if core+extended == len(e) else ''
    intro_names = _pick_names((v['name'] for v in e.values()), 6)
    relation_names = _pick_names((v['label'] for v in r.values()), 5)
    capacity_names = _pick_names((v['name'] for v in domains.values()), 5)
    total_cap = sum(v['count'] for v in domains.values() if v['count'] is not None)
    count_caps = (f'、{total_cap} 项原子能力' if total_cap and all(v['count'] is not None for v in domains.values()) else '')
    first = (f'本资料围绕“{topic}”展开，主要定义{intro_names}等类型，'
             + (f'并描述{relation_names}等关联。' if relation_names else '用于梳理对象及其字段。')
             + '目标是构建可复用的本体类型、属性和关系，不录入具体业务实例。')
    lines = [
        '一、业务与建模目标',
        first,
        '',
        '二、主要建模内容',
        f'• 实体类型：列出 {len(e)} 类{group_desc}。核心对象包括{intro_names}等。'
        + (f'涉及{category_text}等类别。' if category_text else ''),
        f'• 属性定义：已核对 {len(a)} 项共享字段定义，具体挂载需逐项审核。' if a else '• 属性定义：未识别到可直接核对的属性清单；不能据此断言原文没有属性。',
        f'• 关系定义：列出 {len(r)} 条，涉及{relation_names}等对象间的关联。',
    ]
    if domains:
        lines.append(f'• 能力规划：列出 {len(domains)} 个能力域{count_caps}，例如{capacity_names}。能力规划是业务上下文，不直接等同于本体实体或关系。')
    lines.extend(['', '三、需要关注的建模边界',
        '• 只将原文可核验的类型定义交给人工审批；字段、方向或引用有歧义时保留待协商，不生成实例。'])
    source_text='\n'.join(str(c.get('text','')) for d in documents for c in _doc_chunks(d))
    if re.search(r'\bSQL\b|取值SQL|参考数据字段|源表',source_text,re.I):
        lines.append('• 原文出现的数据表或 SQL 作为映射说明，需另外验证；保存本体定义不代表可执行取数。')
    if any(v['extended'] for v in a.values()):
        lines.append('• 部分字段被标为“扩展字段”或“待系统落库回填”，建议保留为待核实的模型候选，不宣称已具备可执行数据映射。')
    if info['note']:
        lines.append((f'• 初稿文字曾写“核心实体14个”，但按实体清单统计为核心 {core} 类、扩展 {extended} 类；建议以实际清单为准。'
                      if core+extended==len(e) else '• '+info['note']))
    questions = []
    if info['note']:
        questions.append(f'是否以实际列出的 {len(e)} 类实体（核心 {core} 类、扩展 {extended} 类）为本轮建模范围？' if core+extended==len(e) else '请确认实体数量及分类口径。')
    if any(v['extended'] for v in a.values()):
        questions.append('标注为扩展或待落库的属性，是否先作为候选保留，暂不承诺数据可执行？')
    questions.append('原子能力及 SQL 仅作为背景和数据映射依据，是否不纳入本次实体、属性和关系定义？' if domains else '是否按现有实体、属性和关系建立候选，跳过业务实例数据？')
    scope = (f'建议主题：{topic}领域本体。\n'
             f'建模范围：依据原文定义的 {len(e)} 类实体、{len(a)} 项属性与 {len(r)} 条关系，'
             '优先对齐并复用 OpenMind 指定分支已有定义；新增内容经过人工确认后再写回。\n'
             '不纳入：具体实例、SQL 执行结果或未经验证的原子能力实现。')
    return {'summary':'\n'.join(lines), 'scope':scope,
            'questions':questions[:5], 'coverage':{
                'complete':True,'strategy':'structured-evidence','structured_tables':info['tables'],
                'entity_definitions':len(e),'attribute_definitions':len(a),'relation_definitions':len(r),
                'capability_domains':len(domains),'atomic_capabilities':total_cap if domains else None,
                'headings':len(info['sections']),'issues':questions[:5],
                'openmind_baseline': ({'entities':len(baseline.get('entities',[])),
                    'attributes':len(baseline.get('attributes',[])),
                    'relations':len(baseline.get('relations',[]))} if baseline is not None else None)
            }}
