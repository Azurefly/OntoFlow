"""Cross-check shared attribute definitions against per-entity attachments.

For BC-like documents, section 5 declares an attribute exactly once; section 6
specifies its direct entity bindings. This is an audit index, not a migration or
an automatic decision to change an existing OpenMind attribute.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from .structured_discovery import _table_cells, _type, _section_owner, _column
from .table_semantics import (canonical_header_index, is_shared_attribute_table,
                               is_attribute_attachment, column_index, ATTRIBUTE_NAME,
                               ATTRIBUTE_CODE, ATTRIBUTE_TYPE, SHARED_OWNERS,
                               OWNER_COUNT, DIRECT_OWNER)


def _rows(documents):
    """Yield column header + source-located rows, independent of order/chapter #.

    Duplicate normalized headers are returned as coverage issues, not selected
    by whichever one happens to be last.
    """
    for doc in documents:
        chunks=doc.get('chunks') or []
        if isinstance(chunks,str):chunks=json.loads(chunks)
        for chunk in chunks:
            if chunk.get('kind')!='table':continue
            lines=chunk.get('text','').splitlines()
            if len(lines)<3:continue
            header=_table_cells(lines[0]);indices,duplicates=canonical_header_index(header)
            for row in lines[2:]:
                if not row.startswith('|'):continue
                cells=_table_cells(row)
                if len(cells)<len(header):continue
                yield str(chunk.get('section','')),header,indices,duplicates,cells,row,chunk.get('locator','')


def build_attribute_catalog(documents):
    """Cross-check all shared definitions and their owners, without fixed chapters.

    The catalog may be missing, malformed, truncated or duplicated. These
    cases must produce explicit issues, never silently return 0/0/0 issues.
    """
    catalog={};assignments=defaultdict(dict);issues=[]
    shared_rows=0;attachment_rows=0;unclassified=0
    for section,header,indices,duplicates,cells,quote,locator in _rows(documents):
        def cell(*names):
            index=column_index(header,*names)
            return cells[index].strip() if 0<=index<len(cells) else ''
        if duplicates:
            issues.append({'code':'','reason':f'表头存在重复列 {sorted(duplicates)}，拒绝选取不确定的属性 CODE/名称', 'locator':locator})
            continue
        # Infer from field declarations, not numbering such as section 5/6.
        has_code=column_index(header,*ATTRIBUTE_CODE)>=0
        has_name=column_index(header,*ATTRIBUTE_NAME)>=0
        if not (has_code and has_name):
            if any(column_index(header,name)>=0 for name in ATTRIBUTE_NAME+ATTRIBUTE_CODE):
                unclassified+=1
                issues.append({'code':'','reason':'疑似属性表缺少名称或编号列，或存在歧义表头；无法完整核对共享属性目录', 'locator':locator})
            continue
        code=cell(*ATTRIBUTE_CODE)
        name=cell(*ATTRIBUTE_NAME)
        if not code or not name:
            issues.append({'code':code,'reason':'属性表记录缺少 CODE 或名称', 'locator':locator})
            continue
        dtype=_type(cell(*ATTRIBUTE_TYPE)) or cell(*ATTRIBUTE_TYPE)
        if is_shared_attribute_table(section,header):
            shared_rows+=1
            owner_column=next((x for x in SHARED_OWNERS if column_index(header,x)>=0),'')
            expected=[x.strip() for x in re.split(r'[、,，]',cell(owner_column)) if x.strip()]
            count_col=next((x for x in OWNER_COUNT if column_index(header,x)>=0),'')
            definition={'code':code,'name':name,'datatype':dtype,
                        'expected_owners':expected,'expected_count':cell(count_col),
                        'quote':quote,'locator':locator}
            if code in catalog:
                old=catalog[code]
                if (old['name'],old['datatype'],old['expected_owners'],old['expected_count']) != (name,dtype,expected,cell(count_col)):
                    issues.append({'code':code,'reason':'同一个共享属性 CODE 存在冲突的定义'})
                continue
            catalog[code]=definition
        elif is_attribute_attachment(section,header):
            attachment_rows+=1
            explicit=next((x for x in DIRECT_OWNER if column_index(header,x)>=0),'')
            owner=cell(explicit) if explicit else _section_owner(section)
            if not owner:
                issues.append({'code':code,'reason':'属性挂载表没有明确的实体归属；拒绝猜测'})
                continue
            record={'name':name,'datatype':dtype,'owner':owner,'quote':quote,'locator':locator}
            existing=assignments[code].get(owner)
            if existing and (existing['name'],existing['datatype'])!=(name,dtype):
                issues.append({'code':code,'reason':f'{owner} 属性名称或类型在重复挂载记录间冲突'})
            assignments[code][owner]=record
        else:
            unclassified+=1
            issues.append({'code':code,'reason':'存在属性名称和 CODE 但无法判定是共享属性目录还是按实体挂载；不可静默跳过', 'locator':locator})
    if assignments and not catalog:
        issues.insert(0,{'code':'','reason':'发现按实体属性挂载，但未识别到任何共享属性目录；覆盖不完整，不能按 0 项目录视为通过'})
    for code,definition in catalog.items():
        got=assignments.get(code,{})
        expected=set(definition['expected_owners']);actual=set(got)
        if expected!=actual:
            issues.append({'code':code,'reason':f'挂载差异，目录={sorted(expected)}，实体清单={sorted(actual)}'})
        if definition['expected_count'].isdigit() and int(definition['expected_count'])!=len(actual):
            issues.append({'code':code,'reason':f'目录声明挂载数 {definition["expected_count"]}，实际 {len(actual)}'})
        for owner,record in got.items():
            if (record['name'],record['datatype'])!=(definition['name'],definition['datatype']):
                issues.append({'code':code,'reason':f'{owner} 属性名称/类型与共享目录不一致'})
    for code in assignments:
        if code not in catalog:
            issues.append({'code':code,'reason':'属性挂载存在，但共享目录没有该 CODE；需先确认定义来源'})
    return {'definitions':catalog,'attachments':dict(assignments),'issues':issues,
            'counts':{'shared_attributes':len(catalog),
                      'direct_bindings':sum(map(len,assignments.values())),
                      'entities_with_properties':len({owner for owners in assignments.values() for owner in owners}),
                      'issues':len(issues)},
            'coverage':{'shared_rows_seen':shared_rows, 'attachment_rows_seen':attachment_rows,
                        'unclassified_rows':unclassified}}


def attribute_identity_from_chunk(chunk, quoted_line, attribute_name=None):
    """Extract authoritative (CODE,name) from a source row by header labels.

    The quoted record must be the *full original row*, present in a table
    whose columns explicitly declare an attribute binding. This is suitable
    for evidence verified by the service, not a user-provided code assertion.
    """
    if chunk.get('kind')!='table':return None
    lines=chunk.get('text','').splitlines()
    if len(lines)<3 or quoted_line not in lines[2:]:return None
    if not is_attribute_attachment(chunk.get('section',''),_table_cells(lines[0])):
        return None
    header=_table_cells(lines[0]);cells=_table_cells(quoted_line)
    code_idx=_column(header,*ATTRIBUTE_CODE)
    name_idx=_column(header,*ATTRIBUTE_NAME)
    if min(code_idx,name_idx)<0 or max(code_idx,name_idx)>=len(cells):return None
    code=cells[code_idx].strip().lower();name=cells[name_idx].strip()
    if not re.fullmatch(r'[a-z][a-z0-9_-]*',code,re.I):return None
    if attribute_name is not None and attribute_name!=name:return None
    return code,name


def verified_source_attribute_code(attribute):
    """Read provenance tied to original column labels, never a positional guess.

    The API-side evidence validator ensures source_code came from a verified
    `属性编号` cell in the SAME quoted row and `属性名称` matches this attribute.
    Historical v1.3.4 rows with no metadata retain their legacy strict
    positional check, only when the locator is the exact known legacy section.
    """
    evidence=attribute.get('evidence') or []
    if isinstance(evidence,str):
        try:evidence=json.loads(evidence)
        except (TypeError,ValueError):return ''
    for item in evidence:
        if not isinstance(item,dict) or item.get('verified') is not True:continue
        locator=str(item.get('locator') or '')
        quote=str(item.get('quote') or '').strip()
        code=str(item.get('source_code') or '').strip().lower()
        if locator.startswith('$.attributes[') and code:
            if (re.fullmatch(r'[a-z][a-z0-9_]*',code) and
                    str(item.get('source_name') or '')==attribute.get('name')):
                return code
            continue
        if not quote.startswith('|'):continue
        name=str(item.get('source_name') or '').strip()
        if code:
            if re.fullmatch(r'[a-z][a-z0-9_-]*',code) and name==attribute.get('name'):
                return code
            continue
        # Backward compatibility with old approved records. This intentionally
        # does not guess column order in new documents.
        if '实体属性（按实体）' not in locator:continue
        cells=_table_cells(quote)
        if len(cells)>=4 and re.fullmatch(r'[a-z][a-z0-9_-]*',cells[1],re.I) and cells[2].strip()==attribute.get('name'):
            return cells[1].lower()
    return ''


def verified_source_object_code(obj,kind):
    """Use exact server-verified native JSON provenance; never guessed CODE."""
    evidence=obj.get('evidence') or []
    if isinstance(evidence,str):
        try:evidence=json.loads(evidence)
        except (ValueError,TypeError):return ''
    prefix={'entity':'$.entities[','relation':'$.relations['}.get(kind)
    if not prefix:return ''
    for item in evidence:
        if not isinstance(item,dict) or item.get('verified') is not True:continue
        if not str(item.get('locator') or '').startswith(prefix):continue
        code=str(item.get('source_code') or '').strip().lower()
        original=str(item.get('source_name') or '')
        expected=obj.get('name') if kind=='entity' else obj.get('predicate')
        if not code or not re.fullmatch(r'[a-z][a-z0-9_]*',code):continue
        if original==expected or (kind=='relation' and code==expected):return code
    return ''
