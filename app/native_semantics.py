"""Canonical native semantics used by preview, execution and reconciliation.

Identity/scope is checked separately. This hash deliberately preserves every
non-transient field, including dependency targets and the entire executionSpec.
Arrays with set semantics are sorted; executable steps retain their order.
"""
from __future__ import annotations
import copy
import hashlib
import json

_TRANSIENT = {'id','branchId','domainId','lockVersion','createdAt','updatedAt',
              'created_at','updated_at','agentProvenance'}
_SET_FIELDS = {'entityIds','sourceEntityIds','targetEntityIds'}

def canonical_semantics(kind, value):
    data=copy.deepcopy({k:v for k,v in value.items() if k not in _TRANSIENT})
    data.setdefault('description','')
    data.setdefault('status','enabled')
    if kind=='entity':
        for key,default in {'parentId':None,'primaryKeyAttributeIds':[],
                            'displayAttributeIds':[],'attributeAliases':{},
                            'fetchLogic':'','remarks':[],'favorite':False}.items():
            data.setdefault(key,default)
    elif kind=='attribute':
        data.setdefault('entityIds',[])
    elif kind=='relation':
        for key,default in {'executionMode':'SEMANTIC','symmetric':False,
                            'associationMode':'entity','steps':'','dependencies':[]}.items():
            data.setdefault(key,default)
        if data.get('executionSpec') is None:data.pop('executionSpec',None)
        data['dependencies']=sorted(data['dependencies'],
            key=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False))
    for key in _SET_FIELDS:
        if key in data:
            data[key]=sorted(set(data[key]))
    return data

def semantic_hash(kind,value):
    return hashlib.sha256(json.dumps(canonical_semantics(kind,value),ensure_ascii=False,
                                    sort_keys=True,separators=(',',':')).encode()).hexdigest()

def semantically_equal(kind,a,b):
    return canonical_semantics(kind,a)==canonical_semantics(kind,b)

def prepare_write(step, existing=None):
    """Produce exactly the full native payload we intend the server to save.

    Binding extension is additive, and only a server-generated reviewed plan
    can authorize it. Never clear fields not owned by the local editor.
    """
    from .openmind import OpenMindError
    kind=step['kind']; data=copy.deepcopy(step['data'])
    if existing is None:return data
    if kind=='entity':
        for k in ('primaryKeyAttributeIds','displayAttributeIds','attributeAliases','fetchLogic','remarks','favorite'):
            data.pop(k,None)
    if kind=='attribute':
        old=set(existing.get('entityIds',[])); new=set(data.get('entityIds',[]))
        additions=new-old
        approved=set(step.get('approved_binding_additions',[]))
        if not old<=new or additions!=approved:
            raise OpenMindError('属性直接绑定变更未通过明确的增量审批，或试图删除既有绑定')
        if (existing.get('dataType')!=data.get('dataType') or existing.get('name')!=data.get('name')):
            raise OpenMindError('共享属性名称/数据类型变化需要独立变更审批')
    return {**copy.deepcopy(existing),**data}
