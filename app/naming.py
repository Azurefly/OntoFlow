"""Separate human-readable Chinese names from stable native ASCII CODEs.

Predicates remain internal semantic tokens (not display labels). No migration
translates or renames remote objects. Unknown labels remain explicitly missing.
"""
from __future__ import annotations
import hashlib
import json
import re

CODE = re.compile(r'^[a-z][a-z0-9_]*$')
HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]')
# Translations of fixed internal vocabulary, not a guess for arbitrary user CODEs.
RELATION_NAMES = {
    'subClassOf': '继承自', 'hasPart': '包含部件', 'partOf': '属于整体',
    'relatedTo': '关联', 'hasComponent': '包含组件', 'locatedAt': '位于',
    'connectsTo': '连接到', 'dependsOn': '依赖', 'worksAt': '任职于',
    'participatesIn': '参与事件', 'initiatesTransfer': '发起转账',
    'hasRecipient': '接收方', 'reportsIncident': '发起报警', 'drives': '驾驶',
    'hasAudit': '具有审核记录', 'belongsTo': '属于', 'uses': '使用',
}
RESERVED = {'class','interface','enum','extends','implements','package','import','return',
            'def','new','null','true','false','this','super','if','else','try','catch',
            'finally','for','while','switch','case','break','continue','throw','throws',
            'public','private','protected','static','final','abstract','synchronized',
            'transient','volatile','void','int','long','float','double','boolean','byte',
            'short','char','instanceof','assert','as','in','trait'}


def is_chinese_name(value):
    return isinstance(value,str) and bool(HAN.search(value))


def code_error(kind, value):
    if not isinstance(value,str) or not CODE.fullmatch(value):
        return '英文 CODE 必须以小写字母开头，仅包含 a-z、0-9、下划线。'
    if len(value)>(64 if kind=='relation' else 255):
        return '关系 CODE 最长64字符，实体和属性 CODE 最长255字符。'
    if value in RESERVED:return 'CODE 不能使用目标系统保留关键字。'
    return ''


def generated_code(kind, local_id, name):
    """Keep legacy ID-based codes stable; this is a placeholder, not translation."""
    stem=re.sub('[^a-z0-9]+','_',str(name).lower()).strip('_')[:32]
    digest=hashlib.sha256(f'{kind}:{local_id}'.encode()).hexdigest()[:12]
    return f'{kind}_{stem}_{digest}' if stem else f'{kind}_{digest}'


def _evidence(record):
    evidence=record.get('evidence') or []
    if isinstance(evidence,str):
        try:evidence=json.loads(evidence)
        except (ValueError,TypeError):return []
    return [x for x in evidence if isinstance(x,dict)] if isinstance(evidence,list) else []


def relation_name(record):
    """Prefer actual label or verified source name; no opaque CODE translations."""
    for key in ('name','label'):
        value=str(record.get(key) or '').strip()
        if is_chinese_name(value):return value
    for e in _evidence(record):
        if e.get('verified') is not True:continue
        value=str(e.get('source_name') or '').strip()
        if is_chinese_name(value):return value
        # Legacy REL key/value row evidence contains the actual Chinese label.
        match=re.fullmatch(r'\s*\|\s*关系\s*\|\s*([^|]+)\|\s*',str(e.get('quote','')))
        if match and is_chinese_name(match.group(1)):
            return match.group(1).strip()
    return RELATION_NAMES.get(str(record.get('predicate','')), '')


def source_code(kind,record):
    from .schema_catalog import verified_source_attribute_code,verified_source_object_code
    value=(verified_source_attribute_code(record) if kind=='attribute'
           else verified_source_object_code(record,kind))
    # Legacy source identifiers may contain a dash. Suggest normalized CODE for
    # a NEW local definition only; remote mappings always take precedence.
    value=re.sub('[^a-z0-9_]+','_',value.lower()) if value else ''
    return value if value and not code_error(kind,value) else ''


def prepare_payload(kind,payload,evidence=(),snapshot=None):
    """Populate fields for preview. Missing Chinese name is fixed by the user."""
    out=dict(payload)
    if out.get('operation')=='delete':return out
    existing=None
    if snapshot and out.get('operation')=='update':
        identity=out.get('record_id') if kind=='relation' else out.get('target_id')
        existing=next((x for x in snapshot[{'entity':'entities','attribute':'attributes','relation':'relations'}[kind]] if x['id']==identity),None)
    if kind=='relation':
        # Empty/missing name from old clients can use a known semantic label.
        label=relation_name({**(existing or {}),**out,'evidence':list(evidence)})
        out['name']=str(out.get('name') or label or '').strip()
        if not out.get('predicate') and out.get('code'):out['predicate']=out['code']
    supplied=out.get('code')
    if not supplied:
        code=(existing or {}).get('code') or source_code(kind,{**out,'evidence':list(evidence)})
        if not code:
            key=json.dumps({'kind':kind,'name':out.get('name',''),
                'owner':out.get('owner_id',''),'source':out.get('source_id',''),
                'target':out.get('target_id','') if kind=='relation' else '',
                'predicate':out.get('predicate','')},ensure_ascii=False,sort_keys=True)
            code=generated_code(kind,key,out.get('predicate') if kind=='relation' else out.get('name',''))
        out['code']=code
    return out


def naming_errors(kind,record):
    errors=[]
    label=relation_name(record) if kind=='relation' else record.get('name','')
    if not is_chinese_name(label):
        errors.append('请补充中文名称；英文 CODE 不能代替中文名称。')
    if code_error(kind,record.get('code')):errors.append(code_error(kind,record.get('code')))
    return errors


def enrich_snapshot(conn,pid,entities,attributes,relations):
    """Read-only legacy fallback; no UUID/code/name rewriting on startup."""
    overrides={(x['kind'],x['local_id']):x['code'] for x in conn.execute(
        'SELECT kind,local_id,code FROM openmind_codes WHERE project_id=?',(pid,)).fetchall()}
    maps={(x['kind'],x['local_id']):x['code'] for x in conn.execute(
        'SELECT kind,local_id,code FROM openmind_mappings WHERE project_id=?',(pid,)).fetchall()}
    labels={x['relation_id']:x['name'] for x in conn.execute(
        'SELECT relation_id,name FROM relation_labels WHERE project_id=?',(pid,)).fetchall()}
    remote={}
    row=conn.execute('SELECT catalog_json FROM openmind_baselines WHERE project_id=?',(pid,)).fetchone()
    if row:
        try:remote={str(x['id']):x for x in json.loads(row['catalog_json']).get('entities',[])}
        except (ValueError,TypeError,KeyError):pass
    for kind,items in (('entity',entities),('attribute',attributes),('relation',relations)):
        for record in items:
            key=(kind,record['id'])
            if kind=='relation':record['name']=labels.get(record['id']) or relation_name(record)
            record['code']=(overrides.get(key) or maps.get(key) or
                (remote.get(record['id'],{}).get('code') if kind=='entity' and record.get('origin')=='openmind' else '') or
                source_code(kind,record) or generated_code(kind,record['id'],record.get('predicate') if kind=='relation' else record.get('name','')))
            record['naming_issues']=naming_errors(kind,record)


def save_metadata(conn,pid,kind,local_id,payload):
    if payload.get('code'):
        conn.execute('INSERT INTO openmind_codes(project_id,kind,local_id,code) VALUES(?,?,?,?) '
            'ON CONFLICT(project_id,kind,local_id) DO UPDATE SET code=excluded.code',
            (pid,kind,local_id,payload['code']))
    if kind=='relation':
        conn.execute('INSERT INTO relation_labels(project_id,relation_id,name) VALUES(?,?,?) '
            'ON CONFLICT(project_id,relation_id) DO UPDATE SET name=excluded.name',
            (pid,local_id,relation_name(payload)))


def assignment_error(kind,payload,evidence,snapshot,baseline=None):
    """Branch-wide identity check; shared Attribute links are the only exception.

    Subclass links map to parentId rather than a native Relation; their semantic
    label/code is local and must not consume a native object code.
    """
    if payload.get('operation')=='delete' or (kind=='relation' and payload.get('predicate')=='subClassOf'):
        return ''
    code=payload['code']
    target=payload.get('record_id') if kind=='relation' else payload.get('target_id')
    from .schema_catalog import verified_source_attribute_code
    trusted=verified_source_attribute_code({**payload,'evidence':evidence}) if kind=='attribute' else ''
    def same_shared(row):
        other=verified_source_attribute_code(row)
        return bool(trusted and other==trusted and row['name']==payload['name'] and row['datatype']==payload['datatype'])
    for other_kind,field in (('entity','entities'),('attribute','attributes'),('relation','relations')):
        for row in snapshot[field]:
            if row['id']==target and other_kind==kind:continue
            if other_kind=='relation' and row.get('predicate')=='subClassOf':continue
            if row.get('code')!=code:continue
            if other_kind==kind=='attribute' and same_shared(row):continue
            return f'英文 CODE「{code}」已被 {row.get("name") or row.get("predicate") or row["id"]} 使用；请确认共享定义或修改编码。'
    for other_kind,field in (('entity','entities'),('attribute','attributes'),('relation','relations'),('action','actions'),('capability','capabilities')):
        for row in (baseline or {}).get(field,[]):
            if str(row.get('code','')).casefold()!=code.casefold():continue
            # Only a verified shared attribute is allowed to add a direct owner.
            if kind==other_kind=='attribute' and trusted and row.get('name')==payload['name'] and row.get('dataType')==payload['datatype']:
                continue
            # Local updates mapped to an existing object are dealt with by the
            # write planner's UUID and lockVersion checks; a read-only anchor may
            # not be changed through this function.
            return f'目标分支的 {other_kind} 已使用 CODE「{code}」，请复用现有对象或选择不同编码。'
    return ''
