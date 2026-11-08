"""Evidence-first Markdown parser and deterministic ontology discovery.

Parsing does not invent definitions. Ambiguous rows are returned as coverage gaps,
not silently converted to accepted native objects. Text positions use source lines.
"""
from __future__ import annotations
import re
from collections import defaultdict
from .table_semantics import (is_background, is_shared_attribute_table,
    is_attribute_attachment, is_entity_attachment_index)

PARSER_VERSION = 'markdown-structure-v4-header-semantic'
DISCOVERY_VERSION = 'evidence-first-v1'

_HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
_SEPARATOR = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$')
_CODE = re.compile(r'^\s*(`{3,}|~{3,})')


def _table_cells(line):
    value = line.strip().strip('|')
    return [v.strip().replace('\\|', '|') for v in re.split(r'(?<!\\)\|', value)]


def _is_table_line(line):
    return '|' in line and len(_table_cells(line)) >= 2


def markdown_chunks(text, max_chars=5200):
    """Keep headings, table headers and rows intact; no fixed 30-line slicing.

    Oversized tables are split between rows and repeat only the header. A single
    oversized source row remains whole for safe downstream handling, rather
    than pretending a split cell has the same meaning as a complete row.
    """
    lines=text.splitlines()
    blocks=[]
    headings={}
    index=0
    while index<len(lines):
        line=lines[index]
        heading=_HEADING.match(line)
        if heading:
            depth=len(heading.group(1));headings={k:v for k,v in headings.items() if k<depth}
            headings[depth]=heading.group(2).strip()
            # Explicit E-/ENTITY-numbered definitions are source evidence too;
            # otherwise skipping headings would silently drop heading-only entities.
            section=' / '.join(headings[k] for k in sorted(headings))
            coded=bool(re.match(r'^(?:E|ENTITY|ENT)[-_ ]?\d+\s*[：:、.． -]+\S',heading.group(2),re.I))
            blocks.append(('definition' if coded else 'heading',index,index+1,section,[line]))
            index+=1
            continue
        if not line.strip():
            index+=1;continue
        section=' / '.join(headings[k] for k in sorted(headings))
        start=index
        code=_CODE.match(line)
        if code:
            fence=code.group(1);index+=1
            while index<len(lines):
                if lines[index].lstrip().startswith(fence[0]*len(fence)):
                    index+=1;break
                index+=1
            blocks.append(('code',start,index,section,lines[start:index]));continue
        # A table is a header followed immediately by a valid Markdown separator.
        if index+1<len(lines) and _is_table_line(line) and _SEPARATOR.match(lines[index+1]):
            index+=2
            while index<len(lines) and _is_table_line(lines[index]) and lines[index].strip():
                index+=1
            raw=lines[start:index]
            header=raw[:2]
            groups=[];group=[]
            for row in raw[2:]:
                if group and len('\n'.join(header+group+[row]))>max_chars:
                    groups.append(group);group=[]
                group.append(row)
            if group:groups.append(group)
            if not groups:groups=[[]]
            offset=start+2
            for group in groups:
                blocks.append(('table',start if offset==start+2 else offset,offset+len(group),section,header+group))
                offset+=len(group)
            continue
        # Prose paragraphs/list items are grouped by section up to max_chars.
        index+=1
        while index<len(lines) and lines[index].strip() and not _HEADING.match(lines[index]) and not _CODE.match(lines[index]):
            if index+1<len(lines) and _is_table_line(lines[index]) and _SEPARATOR.match(lines[index+1]):
                break
            index+=1
        block=lines[start:index]
        if len('\n'.join(block))<=max_chars:
            blocks.append(('prose',start,index,section,block))
        else:
            group=[];begin=start
            for n,row in enumerate(block,start):
                if group and len('\n'.join(group+[row]))>max_chars:
                    blocks.append(('prose',begin,n,section,group));group=[];begin=n
                group.append(row)
            if group:blocks.append(('prose',begin,index,section,group))
    # Coalesce adjacent ordinary paragraphs, but not tables or code blocks.
    merged=[]
    for kind,start,end,section,content in blocks:
        if kind=='prose' and merged and merged[-1][0]=='prose' and merged[-1][3]==section and len('\n'.join(merged[-1][4]+['']+content))<=max_chars:
            last=merged[-1]
            merged[-1]=('prose',last[1],end,section,last[4]+['']+content)
        else:merged.append((kind,start,end,section,content))
    return [{'locator':f'行 {start+1}-{max(start+1,end)}'+(f' / {section}' if section else ''),
             'text':'\n'.join(content), 'kind':kind,'section':section,
             'line_start':start+1,'line_end':end}
            for kind,start,end,section,content in merged if any(x.strip() for x in content)]


def _norm(value):
    return re.sub(r'[\s_\-（）()·.．：:]+','',str(value or '')).casefold()


def _column(header, *words):
    """Prefer exact semantic alias rank over position, rejecting duplicate columns.

    The old code iterated columns first. `实体类型` could outrank `实体名称`
    simply because it was placed earlier, silently inventing class names.
    """
    normalized=[_norm(value) for value in header]
    matches={idx for word in words for idx,value in enumerate(normalized) if value==_norm(word)}
    # Multiple aliases with different columns are not safely interchangeable:
    # never silently choose one because it was positioned first.
    return next(iter(matches)) if len(matches)==1 else -1


def _cell(row,idx):
    return str(row[idx]).strip() if 0<=idx<len(row) else ''


def _type(raw):
    value=raw.strip().casefold().replace(' ','')
    if not value:return ''
    base=re.split(r'[(（<\[\s]',value,maxsplit=1)[0]
    if base in ('string','str','text','varchar','char','字符','字符串','文本','枚举','enum','uuid','id'):
        return 'string'
    if base in ('int','integer','int32','int64','long','short','bigint','smallint','整型','整数'):
        return 'integer'
    if base in ('double','float','number','decimal','numeric','real','浮点','浮点数','小数','数值'):
        return 'double'
    if base in ('bool','boolean','布尔','真假','逻辑'):
        return 'boolean'
    if base in ('date','datetime','timestamp','日期','日期时间','时间','时间戳'):
        return 'date'
    return ''


def _kind_for_table(header):
    e=_column(header,'实体名称','实体名','概念名称','对象名称','entity name','entity','类名')
    a=_column(header,'属性名称','属性名','字段名称','字段名','attribute name','attribute','字段')
    r_source=_column(header,'源实体','来源实体','源对象','source entity','source','主实体')
    r_target=_column(header,'目标实体','目标对象','target entity','target','客实体')
    if r_source>=0 and r_target>=0:return 'relations'
    if a>=0:return 'attributes'
    if e>=0:return 'entities'
    return ''


def _section_owner(section):
    tail=(section or '').split('/')[-1].strip()
    # A heading can be "### E-07 订阅申请的属性定义" or "### 订阅申请属性".
    tail=re.sub(r'^(?:(?:E|ENTITY|ENT)[-_ ]?\d+|\d+(?:[.．]\d+)*)[：:、.． ]*','',tail,flags=re.I)
    tail=re.sub(r'^(?:实体|概念|类)\s*[：:]\s*','',tail)
    tail=re.sub(r'(?:的)?(?:属性|字段|属性定义|字段定义|数据字典|关系定义|关系)\s*$', '',tail)
    # BC per-entity sections are written as "6.1 信源（xy）— 15项".
    # The exact canonical name is "信源"; keep the source code/count in
    # provenance, not as part of an entity lookup key.
    tail=re.sub(r'\s*[（(][a-z][a-z0-9_-]*[）)]\s*[-—–]?\s*\d+\s*项\s*$', '',tail,flags=re.I)

    tail=tail.strip(' ：:-')
    if re.fullmatch(r'(?:实体|概念|属性|关系|字段|对象)(?:定义|列表|清单|目录|表|一览表)?',tail) or re.fullmatch(r'(?:表格|Table|Sheet|工作表)\s*\d*',tail,re.I):
        return ''
    return tail


def deterministic_discover(piece):
    """Return supported clues, unresolved structural records, and semantic-only text.

    Every clue.quote is an exact substring of the source segment; no fabricated
    references or inferred relationship direction are accepted as facts.
    """
    text=str(piece.get('content') or '')
    section=str(piece.get('section') or '')
    kind=str(piece.get('kind') or '')
    results={'entities':[], 'attributes':[], 'relations':[]}
    unresolved=[]
    consumed=set()
    catalog_rows=0
    background_section=is_background(section)
    rows=text.splitlines()
    table_start=next((i for i,line in enumerate(rows[:-1]) if _is_table_line(line) and _SEPARATOR.match(rows[i+1])),None)
    if table_start is not None:
        header=_table_cells(rows[table_start])
        category=_kind_for_table(header)
        # Relationship definitions in many domain design documents are two-column
        # key/value tables beneath a REL-xx heading, rather than wide tables.
        # The section supplies the source identifier; table rows are authoritative
        # for endpoints/direction. Treat conflicts as unresolved, never guess.
        rel_heading=re.search(
            r'(?:^| / )(?:\d+(?:[.．]\d+)*[.．]?\s+)?(REL[-_ ]?\d{1,4})\s*[｜|:：—-]\s*([^｜|]+?)\s*[→➜➡]\s*([^｜|]+?)\s*[｜|:：—-]\s*(.+?)(?:\s*[（(][^）)]*[）)])?$',
            section, re.I)
        if not category and kind == 'table' and rel_heading and _column(header,'项')>=0 and _column(header,'内容')>=0:
            key_col=_column(header,'项'); val_col=_column(header,'内容')
            kv={}; evid={}
            for i in range(table_start+2,len(rows)):
                line=rows[i]
                if not _is_table_line(line):continue
                fields=_table_cells(line)
                key=_cell(fields,key_col);value=_cell(fields,val_col)
                if key and value:kv[key]=value;evid[key]=line
                consumed.add(i)
            consumed.update((table_start,table_start+1))
            src,dst,label=(kv.get('实体A','').strip(),kv.get('实体B','').strip(),kv.get('关系','').strip())
            hsrc,hdst,hlabel=(rel_heading.group(2).strip(),rel_heading.group(3).strip(),rel_heading.group(4).strip())
            if src and dst and label and (src,dst,label)==(hsrc,hdst,hlabel):
                code='rel_'+re.search(r'\d+',rel_heading.group(1)).group(0).zfill(2)
                results['relations'].append({
                    'source':src,'target':dst,'predicate':code,'code':code,
                    'label':label,'description':f'{label}（{rel_heading.group(1).upper()}；{kv.get("基数", "") }）',
                    'quote':evid['关系']})
            else:
                unresolved.append({'source_excerpt':' '.join(text.split())[:160],
                    'reason':'REL 关系标题和实体A/实体B/关系定义不一致，或端点缺失；必须人工校验'})
        elif category == 'entities' and is_entity_attachment_index(section,header):
            # Section 6 starts with an entity-to-attribute count index. The
            # authoritative entity definition is the section 4 catalog.
            # Re-ingesting its rows would yield 44 suggestions for 22 classes.
            consumed.update(range(table_start,len(rows)))
        elif category=='attributes' and is_shared_attribute_table(section,header):
            # This is the shared attribute catalog. Per-entity ownership is
            # declared later in section 6; parsing the same catalog again would
            # generate hundreds of redundant candidate writes. Count and retain
            # the catalog as covered cross-reference, not missing properties.
            catalog_rows=sum(1 for row in rows[table_start+2:] if _is_table_line(row))
            consumed.update(range(table_start,len(rows)))
        elif category=='attributes' and _column(header,'属性编号')>=0 and not (
                is_attribute_attachment(section,header) or _column(header,'所属实体','归属实体','实体名称','实体','对象名称','归属对象','owner')>=0):
            unresolved.append({'source_excerpt':rows[table_start][:160],
                'reason':'属性表有名称和编码，但缺少可信的所属实体或共享挂载列；不能猜测归属，需人工核查'})
            consumed.update((table_start,table_start+1))
        elif category:
            consumed.update((table_start,table_start+1))
            for i in range(table_start+2,len(rows)):
                line=rows[i]
                if not _is_table_line(line):continue
                consumed.add(i)
                fields=_table_cells(line)
                if len(fields)<len(header):
                    unresolved.append({'source_excerpt':line[:160], 'reason':'表格字段数量少于表头；需要人工核查'});continue
                if category=='entities':
                    name=_cell(fields,_column(header,'实体名称','实体名','概念名称','对象名称','entity name','entity','类名'))
                    if not name:
                        unresolved.append({'source_excerpt':line[:160],'reason':'实体行缺少实体名称'});continue
                    results['entities'].append({'name':name,'code':_cell(fields,_column(header,'实体编号','实体编码','实体code','code','编号')),
                        'description':_cell(fields,_column(header,'描述','说明','定义','实体描述','业务含义','含义')),
                        'quote':line})
                elif category=='attributes':
                    name=_cell(fields,_column(header,'属性名称','属性名','字段名称','字段名','attribute name','attribute','字段'))
                    owner=_cell(fields,_column(header,'所属实体','归属实体','实体名称','实体','对象名称','归属对象','owner')) or _section_owner(section)
                    rawtype=_cell(fields,_column(header,'数据类型','属性类型','字段类型','datatype','data type','type','类型'))
                    if not name or not owner:
                        unresolved.append({'source_excerpt':line[:160],'reason':'属性行缺少名称或所属实体；暂不自动归属'});continue
                    if rawtype and not _type(rawtype):
                        unresolved.append({'source_excerpt':line[:160],'reason':f'属性 {name} 的数据类型「{rawtype}」不可安全映射到 OpenMind 五种类型'})
                    results['attributes'].append({'owner':owner,'name':name,'code':_cell(fields,_column(header,'属性编号','属性编码','属性CODE','字段编号','字段编码','attribute code','code','编号')),
                        'datatype':_type(rawtype) or rawtype or '',
                        'raw_datatype':rawtype, 'description':_cell(fields,_column(header,'描述','说明','定义','业务含义')),'quote':line})
                else:
                    source=_cell(fields,_column(header,'源实体','来源实体','源对象','source entity','source','主实体'))
                    target=_cell(fields,_column(header,'目标实体','目标对象','target entity','target','客实体'))
                    predicate=_cell(fields,_column(header,'关系名称','关系编码','关系类型','谓词','predicate','relation','关系'))
                    if not source or not target or not predicate:
                        unresolved.append({'source_excerpt':line[:160],'reason':'关系行缺少源实体、目标实体或关系标识；禁止猜测方向'});continue
                    results['relations'].append({'source':source,'target':target,'predicate':predicate,
                        'code':_cell(fields,_column(header,'关系编号','关系code','code','编号')),
                        'description':_cell(fields,_column(header,'说明','定义','描述','业务含义')),'quote':line})
        elif background_section:
            # Capability/SQL definition tables are background, not new class,
            # property or relation candidates. They remain searchable evidence.
            consumed.update(range(table_start,len(rows)))
        else:
            unresolved.append({'source_excerpt':rows[table_start][:160],'reason':'未知表格结构；需模型识别或人工指定字段含义'})
    for i,line in enumerate(rows):
        if i in consumed or not line.strip():continue
        if kind=='definition':
            match=re.match(r'^\s*#{1,6}\s+(?P<code>(?:E|ENTITY|ENT)[-_ ]?\d+)\s*[:：、.． -]+(?P<name>[^（(：:]+)',line,re.I)
            if match:
                results['entities'].append({'name':match.group('name').strip(),'code':match.group('code'),
                    'description':'','quote':line})
                consumed.add(i);continue
        if line.lstrip().startswith(('#','```','~~~')):continue
        m=re.match(r'^\s*(?:[-*+]\s+)?(?:实体|概念|类)\s*[：:]\s*(.+?)\s*$',line)
        if m:
            names=[n.strip() for n in re.split(r'[、，,;；]+',m.group(1)) if n.strip()]
            results['entities'].extend({'name':name,'description':'','quote':line} for name in names)
            consumed.add(i);continue
        m=re.match(r'^\s*(?:[-*+]\s+)?(?:属性|字段)\s*[：:]\s*(.+?)[.。·]\s*([^:：]+)\s*[：:]\s*([\w\u4e00-\u9fff]+)\s*$',line)
        if m:
            rawtype=m.group(3)
            results['attributes'].append({'owner':m.group(1).strip(),'name':m.group(2).strip(),
                'datatype':_type(rawtype) or rawtype,'raw_datatype':rawtype,'quote':line})
            consumed.add(i);continue
        m=re.match(r'^\s*(?:[-*+]\s+)?关系\s*[：:]\s*(\S+)\s+(\S+)\s+(\S+)\s*$',line)
        if m:
            results['relations'].append({'source':m.group(1),'predicate':m.group(2),'target':m.group(3),'quote':line})
            consumed.add(i)
    # A clearly structured block needs no LLM re-reading. Other prose remains
    # available for semantic inference; headings and separator lines are context.
    semantic='\n'.join(line for i,line in enumerate(rows) if i not in consumed and line.strip() and
                       not line.lstrip().startswith(('#','```','~~~')))
    if kind=='table' and any(re.search(r'缺少|缺失|不一致|未知表格|不能猜测归属',g.get('reason','')) for g in unresolved):
        semantic=text  # retain table header + evidence context for semantic clarification
    if kind=='heading':semantic=''
    # Navigational markup and catalog prefaces are not unextracted ontology
    # definitions. They remain in source/evidence but cost zero model calls.
    bare=semantic.strip()
    content_lines=[line.strip() for line in bare.splitlines() if line.strip()
                   and not re.fullmatch(r'<a\s+id=["\']?[^>]+></a>',line.strip(),re.I)]
    if (not content_lines or
        all(re.match(r'^[-*]\s+\[[^]]+\]\(#[^)]+\)\s*$', line)
            for line in content_lines) or
        bare.startswith('> 本文档由《') or
        re.match(r'^(?:按实体初稿清单\s*\d+|全量实体属性集合|按实体分组展示属性挂载|每条关系给出实体A)', bare)):
        semantic=''
    # A section explicitly named as capabilities, SQL, validation or revisions
    # provides business background, not ontology schema definitions. Keep it in
    # the document for retrieval/coverage, but never repeatedly send it to the
    # entity/attribute/relation model as if it were a definition.
    if background_section:
        semantic=''
    if kind=='code':
        semantic='';unresolved.append({'source_excerpt':text[:120], 'reason':'代码围栏未作为本体定义，需人工核查是否包含权威定义'})
    return {**results,'_unresolved':unresolved,'_semantic_text':semantic,
            '_coverage':{'structured_rows':len(consumed),
                         'rules_entities':len(results['entities']),
                         'rules_attributes':len(results['attributes']),
                         'rules_relations':len(results['relations']),
                         'unknown_records':len(unresolved),
                         'catalog_reference_rows':catalog_rows,
                         'background_only':background_section}}


def tabular_chunks(numbered_rows, label, max_chars=5000):
    """Convert CSV/XLSX/DOCX table rows into source-located Markdown tables.

    A header is repeated across row-boundary chunks, preserving column semantics.
    Single-row/unnamed tables are retained as prose, never dropped.
    """
    records=[(n,[('' if x is None else str(x)).replace('|', r'\|').replace('\n','; ') for x in values])
             for n,values in numbered_rows if any(x is not None and str(x).strip() for x in values)]
    if not records:return []
    if len(records)<2:
        n,fields=records[0]
        return [{'kind':'prose','section':label,'locator':f'{label} 行 {n}',
                 'line_start':n,'line_end':n,'text':' | '.join(fields)}]
    colcount=max(max(len(v) for _,v in records),2)
    ambiguous=(len({len(v) for _,v in records})>1 or
               len(set(records[0][1]))<len(records[0][1]) or any(not v.strip() for v in records[0][1]))
    def render(values):
        return '| '+' | '.join((values+['']*colcount)[:colcount])+' |'
    header=render(records[0][1]);separator='| '+' | '.join('---' for _ in range(colcount))+' |'
    out=[];group=[];begin=None
    for number,values in records[1:]:
        row=render(values)
        if group and len('\n'.join([header,separator]+group+[row]))>max_chars:
            out.append({'kind':'table','section':label,
                        'locator':f'{label} 行 {begin}-{prev}',
                        'line_start':begin,'line_end':prev,
                        'text':'\n'.join([header,separator]+group)})
            group=[];begin=None
        if begin is None:begin=number
        group.append(row);prev=number
    if group:
        out.append({'kind':'table','section':label,
                    'locator':f'{label} 行 {begin}-{prev}',
                    'line_start':begin,'line_end':prev,
                    'text':'\n'.join([header,separator]+group)})
    if ambiguous:
        for chunk in out:
            chunk['kind']='ambiguous_table'
            chunk['layout_warning']='表头重复/缺失或各行列数不一致，保留全部单元格，归属需人工核查'
    return out
