"""Read-only evidence for sync conflicts; no implicit overwrite or blind replay."""
from __future__ import annotations
import hashlib
import json
from .native_semantics import canonical_semantics,semantically_equal

PLURAL={'entity':'entities','attribute':'attributes','relation':'relations'}
UNRESOLVED=('PREPARED','UNKNOWN','ACKNOWLEDGED','CONFLICT')

def observation_hash(operation,actual):
    # Include optimistic concurrency and raw readback, not merely a semantic
    # hash. Even semantically equivalent version changes demand a fresh review.
    value={'operation':operation['id'],'state':operation['state'],
           'desired':operation['request_hash'],'updated':operation['updated_at'],
           'actual':actual}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def operation_view(operation,actual):
    desired=json.loads(operation['desired']);before=json.loads(operation['before_json'])
    kind=operation['kind']
    expected=canonical_semantics(kind,desired)
    observed=canonical_semantics(kind,actual) if actual is not None else {}
    differences=[]
    if actual is None:
        differences.append({'field':'$object','expected':'该 UUID 的对象存在','actual':'当前目录没有该 UUID','before_present':bool(before)})
    else:
        for key in sorted(set(expected)|set(observed)):
            if key not in expected or key not in observed or expected.get(key)!=observed.get(key):
                differences.append({'field':key,'expected':expected.get(key),
                                    'actual':observed.get(key),'expected_present':key in expected,
                                    'actual_present':key in observed})
    same=actual is not None and semantically_equal(kind,actual,desired)
    unchanged=(actual is None and not before) or (actual is not None and before and semantically_equal(kind,actual,before)
                                                 and actual.get('lockVersion')==before.get('lockVersion'))
    identity_ok=actual is None or (str(actual.get('id'))==operation['remote_id']
                                 and actual.get('code')==operation['code']
                                 and actual.get('branchId',operation['branch_id'])==operation['branch_id'])
    return {'id':operation['id'],'kind':kind,'name':desired.get('name',operation['code']),
            'code':operation['code'],'remote_id':operation['remote_id'],'state':operation['state'],
            'revision':operation['revision'],'attempts':operation['attempts'],
            'error':operation['error'],'updated_at':operation['updated_at'],
            'matches_approved':same,'matches_before':bool(unchanged),
            'remote_lock_version':actual.get('lockVersion') if actual else None,
            'differences':differences,'observation_hash':observation_hash(operation,actual),
            'can_return_to_review':identity_ok and operation['state']=='CONFLICT',
            'hint':'可回读确认已成功，不需要再写入' if same else
                   '当前回读与写入前一致，可以安全解除旧冻结，但不在本操作中发送写入' if unchanged else
                   '存在真实内容差异。可保留当前远端、撤销旧写入授权并重新人工审核；本操作不会覆盖远端'}

def conflict_summary(operation,actual):
    view=operation_view(operation,actual)
    fields='、'.join(item['field'] for item in view['differences'][:6]) or '尚需回读'
    return (f'远端写入冲突：{view["kind"]}「{view["name"]}」（CODE={view["code"]}）；'
            f'差异字段：{fields}。请打开“OpenMind 对接 → 写入冲突与恢复”查看并重新核对。'
            f'操作编号：{operation["id"]}')
