"""Semantic identity for pending ontology definitions across source sections.

Descriptions and evidence are not identity fields. Conflicting schema definitions
must be clarified, not accepted as extra duplicate candidates.
"""
from __future__ import annotations
import re


def _normalize(value):
    return re.sub(r'[\s\u3000]+','',str(value or '')).casefold()


def proposal_identity(kind, payload):
    if payload.get('operation','add')!='add':
        return kind,'change',payload.get('operation'),payload.get('record_id') or payload.get('target_id')
    if kind=='entity':return kind,_normalize(payload.get('name'))
    if kind=='attribute':return kind,str(payload.get('owner_id','')),_normalize(payload.get('name'))
    if kind=='relation':return kind,str(payload.get('source_id','')),_normalize(payload.get('predicate')),str(payload.get('target_id',''))
    return kind,''


def compatibility_issue(kind, old, new):
    if old.get('code') and new.get('code') and old['code']!=new['code']:
        return '同一候选的英文 CODE 不一致，需要人工确认'
    if kind=='relation' and old.get('name') and new.get('name') and old['name']!=new['name']:
        return '同一关系的中文名称不一致，需要人工确认'
    if kind=='attribute' and old.get('datatype')!=new.get('datatype'):
        return '同一实体上的同名属性数据类型冲突，需人工确认后再提交'
    if kind=='attribute' and bool(old.get('multivalued'))!=bool(new.get('multivalued')):
        return '同一属性的单值/多值定义冲突，需要人工确认'
    desc1=_normalize(old.get('description',''))
    desc2=_normalize(new.get('description',''))
    if desc1 and desc2 and desc1!=desc2:
        return '同一语义候选的描述不一致，已保留首条，另一处证据需人工核对'
    return ''


def merge_evidence(existing, additional, max_count=10):
    rows=[];seen={}
    for item in list(existing)+list(additional):
        if not isinstance(item,dict):continue
        key=(str(item.get('document_id','')),str(item.get('locator','')),str(item.get('quote','')))
        if key in seen:
            old=rows[seen[key]]
            # A v1.3.4 record may have verified provenance without the source
            # column metadata. The upgraded service re-verifies the full row:
            # preserve that stricter CODE proof when merging identical evidence.
            if (item.get('verified') is True and item.get('source_code') and item.get('source_name')
                    and not old.get('source_code')):
                rows[seen[key]]=item
            continue
        if len(rows)>=max_count:break
        seen[key]=len(rows);rows.append(item)
    return rows
