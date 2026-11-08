"""Versioned human modeling intent and proposal provenance.

Scope choices are human decisions, NOT source claims. Context only controls
candidate interpretation; it never mutates approved ontology or remote data.
"""
from __future__ import annotations
import copy
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator
from .store import j, now, uid

POLICY_VERSION='intent-aware-v1'
FIELDS=('goal','include_types','exclude_types','role_strategy','instructions','reading_mode')
DEFAULT={'goal':'','include_types':[],'exclude_types':[], 'role_strategy':'ask',
         'instructions':'','reading_mode':'auto'}
LABELS={'source_explicit':'原文明示','inference':'建模推断','user_decision':'用户决定','legacy':'历史候选（未分类）'}

class ModelingIntentIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version:int=Field(ge=0)
    goal:str=Field(default='',max_length=2000)
    include_types:list[str]=Field(default_factory=list,max_length=100)
    exclude_types:list[str]=Field(default_factory=list,max_length=100)
    role_strategy:Literal['ask','relations','types']='ask'
    instructions:str=Field(default='',max_length=4000)
    reading_mode:Literal['auto','semantic_review']='auto'
    note:str=Field(default='',max_length=2000)
    @field_validator('include_types','exclude_types')
    @classmethod
    def valid_terms(cls,values):
        result=[]
        for raw in values:
            name=raw.strip()
            if not name or len(name)>255:raise ValueError('类型名称需为 1–255 字')
            if name not in result:result.append(name)
        return result


def digest(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def context_in_tx(c,pid):
    row=c.execute('SELECT * FROM modeling_contexts WHERE project_id=?',(pid,)).fetchone()
    if row:
        spec=json.loads(row['spec'])
        return {**DEFAULT,**spec,'version':row['version'],'confirmed':True,
                'updated_at':row['updated_at']}
    return {**copy.deepcopy(DEFAULT),'version':0,'confirmed':False,'updated_at':None}


def runtime_in_tx(c,pid):
    p=c.execute('SELECT scope FROM projects WHERE id=?',(pid,)).fetchone()
    intent=context_in_tx(c,pid)
    binding=c.execute('SELECT domain_id,branch_id FROM openmind_bindings WHERE project_id=?',(pid,)).fetchone()
    base=c.execute('SELECT fingerprint FROM openmind_baselines WHERE project_id=?',(pid,)).fetchone()
    result={key:intent[key] for key in FIELDS}
    result.update(project_id=pid,confirmed_scope=p['scope'] if p else '',
        target_branch=dict(binding) if binding else None,
        baseline_fingerprint=base['fingerprint'] if base else '',policy_version=POLICY_VERSION)
    result['fingerprint']=digest(result)
    result['decision_version']=intent['version']
    return result


def context(store,pid):
    with store.tx(read_only=True) as c:return context_in_tx(c,pid)


def runtime(store,pid):
    with store.tx(read_only=True) as c:return runtime_in_tx(c,pid)


def model_config(store,pid,config):
    return {**config,'interpretation':runtime(store,pid)}


def neutral_basis(clues,kind,method,rationale):
    out=copy.deepcopy(clues)
    for category in ('entities','attributes','relations'):
        for item in out.get(category,[]):
            item['_basis']={'kind':kind,'method':method,'rationale':rationale}
    return out


def apply_intent(clues,intent):
    """Apply only explicit label filters and recorded role policy, not keywords.

    Arbitrary goal text informs the LLM; rules do not pretend to understand its
    meaning. Exact include/exclude lists are the user's machine-readable choice.
    No source clue is deleted from the underlying evidence cache.
    """
    out=copy.deepcopy(clues)
    if not intent:return out
    include=set(intent.get('include_types') or [])
    exclude=set(intent.get('exclude_types') or [])
    role_strategy=intent.get('role_strategy','ask')
    decisions=list(out.get('_intent_applied',{}).get('decisions',[]))
    roles={'报警人','报案人','驾驶人'}
    inferred_roles=set()
    for ent in out.get('entities',[]):
        if ent.get('name') in roles and ent.get('_basis',{}).get('kind')=='inference':
            inferred_roles.add(ent['name'])
    def relevant(item,kind):
        owners=[item.get('name','')] if kind=='entities' else [item.get('owner','')] if kind=='attributes' else [item.get('source',''),item.get('target','')]
        if any(name in exclude for name in owners):return False,'用户明确排除的类型'
        if include and any(name not in include for name in owners):return False,'不在用户明确选择的类型集合'
        if kind=='entities' and role_strategy=='relations' and item.get('name') in inferred_roles:
            return False,'用户选择复用自然人并通过行为关系表达角色'
        return True,''
    for category in ('entities','attributes','relations'):
        keep=[]
        for item in out.get(category,[]):
            allowed,reason=relevant(item,category)
            if allowed:keep.append(item)
            else:decisions.append({'kind':category,'name':item.get('name') or item.get('predicate') or '',
                                  'reason':reason,'basis':'user_decision'})
        out[category]=keep
    if role_strategy in ('relations','types'):
        # These questions are a modeling choice, not an incomplete source read.
        remaining=[]
        for note in out.get('_unresolved',[]):
            if (note.get('category')=='modeling_choice' and
                any(role in note.get('reason','') for role in inferred_roles)):
                decisions.append({'kind':'clarification','reason':note.get('reason',''),
                                  'applied':role_strategy,'basis':'user_decision'})
            else:remaining.append(note)
        out['_unresolved']=remaining
    decisions=list({j(decision):decision for decision in decisions}.values())
    out['_intent_applied']={'fingerprint':intent.get('fingerprint',''),
                           'decision_version':intent.get('decision_version',0),
                           'omitted_candidates':len([x for x in decisions if x['kind']!='clarification']),
                           'decisions':decisions}
    for category in ('entities','attributes','relations'):
        for item in out.get(category,[]):
            basis=item.setdefault('_basis',{'kind':'inference','method':'unknown','rationale':'未分类的建模建议'})
            basis['intent_fingerprint']=intent.get('fingerprint','')
    return out


def save_proposal_origin(c,pid,proposal_id,basis,fp):
    c.execute('''INSERT INTO proposal_origins(proposal_id,project_id,basis,context_fingerprint,updated_at)
       VALUES(?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET basis=excluded.basis,
       context_fingerprint=excluded.context_fingerprint,updated_at=excluded.updated_at''',
       (proposal_id,pid,j(basis),fp,now()))


def public_origin(c,pid,proposal_id,current_fp):
    row=c.execute('SELECT basis,context_fingerprint FROM proposal_origins WHERE proposal_id=? AND project_id=?',
                  (proposal_id,pid)).fetchone()
    if not row:return {'kind':'legacy','label':LABELS['legacy'],
                       'context_stale':context_in_tx(c,pid).get('version',0)>0}
    data=json.loads(row['basis'])
    return {**data,'label':LABELS.get(data.get('kind'),'建模推断'),
        'context_stale':bool(row['context_fingerprint'] and row['context_fingerprint']!=current_fp)}


def payload_scope_reason(kind,payload,snapshot,intent,basis_kind='inference'):
    """The synchronous/chat proposal routes obey the same exact scope policy.

    This checks only explicit label decisions. It does not pretend arbitrary
    free-text goals are a deterministic semantic filter.
    """
    entities={str(e['id']):e.get('name','') for e in snapshot.get('entities',[])}
    if kind=='entity':names=[payload.get('name','')]
    elif kind=='attribute':names=[entities.get(str(payload.get('owner_id','')),'')]
    elif kind=='relation':names=[entities.get(str(payload.get('source_id','')),''),
                               entities.get(str(payload.get('target_id','')),'')]
    else:return ''
    include=set(intent.get('include_types') or []);exclude=set(intent.get('exclude_types') or [])
    if any(name in exclude for name in names):return '用户已明确排除此类型'
    if include and any(name not in include for name in names):return '不在用户明确选择的类型集合中'
    if (kind=='entity' and basis_kind=='inference' and intent.get('role_strategy')=='relations'
        and payload.get('name') in ('报警人','报案人','驾驶人')):
        return '用户已选择通过自然人行为关系表达角色，不新增推断出的角色类'
    return ''
