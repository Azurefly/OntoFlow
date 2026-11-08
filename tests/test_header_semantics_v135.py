"""V1.3.5: regression against frozen, independent metamorphic audit.

The expected sets are independently reconstructed from current input, never
hardcoded as BC-specific counts. BC file is NOT bundled in release archives.
"""
import os
import pytest
from pathlib import Path
import sys
import tempfile

import pytest

from app.extraction import extract
from app.structured_discovery import markdown_chunks, deterministic_discover
from app.schema_catalog import (build_attribute_catalog, attribute_identity_from_chunk,
                                verified_source_attribute_code)
from app.table_semantics import headings
from app.openmind import make_plan

BC=Path(os.getenv('ONTOFLOW_BC_FIXTURE','/mnt/data/信息分发本体设计 (BC 项目)-v0.1.md'))


def _table(text):
    return next(x for x in markdown_chunks(text) if x['kind']=='table')


def test_entity_name_not_confused_with_entity_type_when_columns_reverse():
    src='''# 设备本体
## 类型清单
| 实体类型 | 描述 | 实体编号 | 实体名称 | 实体级别 |
|---|---|---|---|---|
| 主体类 | 申请主体 | sqz | 申请者 | 核心 |
| 资源类 | 信息资源 | zyx | 资源项 | 核心 |
'''
    c=_table(src)
    found=deterministic_discover({'content':c['text'],'section':c['section'],'kind':'table'})
    assert {(x['code'],x['name']) for x in found['entities']}=={('sqz','申请者'),('zyx','资源项')}
    assert not any(x['name'] in ('主体类','资源类') for x in found['entities'])


def test_duplicated_entity_header_is_ambiguous_and_requires_review():
    src='''## 实体表
| 实体名称 | 实体名称 | 实体编号 |
|---|---|---|
| 张 | 李 | ab |
'''
    c=_table(src)
    result=deterministic_discover({'content':c['text'],'kind':'table','section':c['section']})
    assert not result['entities']
    assert result['_unresolved'] and result['_semantic_text']


def test_chapter_number_is_not_part_of_table_role():
    src='''# 库存本体
## 27. 属性定义
| 属性编号 | 属性名称 | 挂载实体 | 挂载实体数 | 数据类型 |
|---|---|---|---|---|
| cjsj | 创建时间 | 仓库、仓库管理员 | 2 | 字符串 |
## 39. 实体属性（按实体）
### 39.1 仓库（ck）— 1项
| 序号 | 属性名称 | 属性编号 | 数据类型 |
|---|---|---|---|
| 1 | 创建时间 | cjsj | 字符串 |
### 39.2 仓库管理员（gly）— 1项
| 序号 | 属性名称 | 属性编号 | 数据类型 |
|---|---|---|---|
| 1 | 创建时间 | cjsj | 字符串 |
'''
    chunks=markdown_chunks(src)
    catalog=build_attribute_catalog([{'chunks':chunks}])
    assert catalog['counts']=={'shared_attributes':1,'direct_bindings':2,
                               'entities_with_properties':2,'issues':0}
    all_found=[deterministic_discover({'content':x['text'],'kind':x['kind'],'section':x['section']})
               for x in chunks if x['kind']!='heading']
    assert len([a for group in all_found for a in group['attributes']])==2
    assert not [group['_semantic_text'] for group in all_found if group['_semantic_text'].strip()]


def test_catalog_missing_is_never_zero_issues():
    src='''## 29. 实体属性（按实体）
### 29.1 仓库（ck）— 1项
| 序号 | 属性名称 | 属性编号 | 数据类型 |
|---|---|---|---|
| 1 | 创建时间 | cjsj | 字符串 |
'''
    result=build_attribute_catalog([{'chunks':markdown_chunks(src)}])
    assert result['counts']['shared_attributes']==0
    assert result['counts']['direct_bindings']==1
    assert result['counts']['issues']>=1
    assert any('共享属性目录' in x['reason'] for x in result['issues'])


def test_ambiguous_attribute_table_is_not_silently_dropped():
    src='''# 某领域本体
## 字段约束
| 属性编号 | 属性名称 | 数据类型 |
|---|---|---|
| x | 名称 | 字符串 |
'''
    catalog=build_attribute_catalog([{'chunks':markdown_chunks(src)}])
    assert catalog['counts']['issues']>0
    assert '无法判定' in catalog['issues'][0]['reason']


def test_verified_native_attribute_source_code_not_positional():
    src='''## 本体字段挂载
### 仓库（ck）— 1项
| 属性名称 | 属性角色 | 数据类型 | 属性编号 | 是否必填 |
|---|---|---|---|---|
| 创建时间 | 通用 | 字符串 | cjsj | 否 |
'''
    chunk=_table(src)
    quote=chunk['text'].splitlines()[2]
    assert attribute_identity_from_chunk(chunk,quote)==('cjsj','创建时间')
    assert attribute_identity_from_chunk(chunk,quote,'其他名称') is None
    evidence=[{'document_id':'doc','locator':chunk['locator'],'quote':quote,'verified':True,
               'source_code':'cjsj','source_name':'创建时间'}]
    assert verified_source_attribute_code({'name':'创建时间','evidence':evidence})=='cjsj'
    assert verified_source_attribute_code({'name':'修订时间','evidence':evidence})==''


def test_conflicting_duplicate_shared_code_is_a_gap():
    src='''## 全量属性目录
| 属性编号 | 属性名称 | 挂载实体 | 数据类型 |
|---|---|---|---|
| cjsj | 创建时间 | 仓库 | 字符串 |
| cjsj | 创建日期 | 仓库 | 日期 |
'''
    catalog=build_attribute_catalog([{'chunks':markdown_chunks(src)}])
    assert catalog['counts']['issues']>=1
    assert any('冲突' in x['reason'] for x in catalog['issues'])


def test_other_domain_and_unrelated_section_numbers():
    src='''## 103. 门店本体清单
| 实体名称 | 实体编号 | 实体类型 |
|---|---|---|
| 门店 | store | 组织类 |
| 客户 | client | 主体类 |
## 801. 全量属性目录
| 属性编号 | 属性名称 | 挂载实体 | 数据类型 |
|---|---|---|---|
| cjsj | 创建时间 | 门店、客户 | 日期 |
## 5. 业务按实体属性
### 5.1 门店（store）— 1项
| 属性编号 | 属性名称 | 数据类型 |
|---|---|---|
| cjsj | 创建时间 | 日期 |
### 5.2 客户（client）— 1项
| 属性编号 | 属性名称 | 数据类型 |
|---|---|---|
| cjsj | 创建时间 | 日期 |
'''
    chunks=markdown_chunks(src)
    results=[deterministic_discover({'content':p['text'],'kind':p['kind'],'section':p['section']})
             for p in chunks if p['kind']!='heading']
    assert {e['name'] for group in results for e in group['entities']}=={'门店','客户'}
    cat=build_attribute_catalog([{'chunks':chunks}])
    assert cat['counts']['shared_attributes']==1
    assert cat['counts']['direct_bindings']==2
    assert cat['counts']['issues']==0


def test_bc_independent_audit_all_frozen_mutations_if_available():
    if not BC.is_file():pytest.skip('BC real source is not included with distributable tests')
    # Import independent oracle; no hardcoded answers in the oracle itself.
    from scripts.ontoflow_independent_audit import (raw_reference,production_extract,check,
                                            swap_catalog_columns,renumber_headings,append_valid_entity)
    original=BC.read_text(encoding='utf-8')
    variants=[('original',original),('renumber',renumber_headings(original)),
              ('new_entity',append_valid_entity(original))]
    variants.extend((f'seed{i}',swap_catalog_columns(original,i)[0]) for i in range(20))
    for label,text in variants:
        ref=raw_reference(text);actual=production_extract(text);verified=check(text,ref,actual)
        assert all(verified[k]['exact_set_match'] for k in
                   ('entities','per_entity_attributes','relations_source_target_label')), label
        assert not verified['evidence_bad_quotes'],label
        assert actual['counts'].get('semantic_units',0)==0,label
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'variant.md';p.write_text(text,encoding='utf-8')
            catalog=build_attribute_catalog([{'chunks':extract(p)['chunks']}])
        assert catalog['counts']['shared_attributes']==len(ref['shared']),label
        assert catalog['counts']['direct_bindings']==len(ref['attachments']),label
        assert catalog['counts']['issues']==0,label


def test_real_api_evidence_uses_header_code_not_client_claim_or_column_position(tmp_path,monkeypatch):
    """Upload -> manual accept -> durable evidence -> native shared Attribute."""
    import json
    import uuid
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv('APP_ENV','development')
    monkeypatch.delenv('API_TOKEN',raising=False)
    monkeypatch.delenv('DATABASE_URL',raising=False)
    src='''# 仓储本体
## 实体定义
| 实体名称 | 实体编号 | 实体类型 |
|---|---|---|
| 仓库 | ck | 组织类 |
| 仓库管理员 | gly | 自然人类 |
## 共享属性定义
| 挂载实体 | 属性名称 | 属性编号 | 数据类型 |
|---|---|---|---|
| 仓库、仓库管理员 | 创建时间 | cjsj | 字符串 |
## 实体属性（按实体）
### 1. 仓库（ck）— 1项
| 属性名称 | 属性编号 | 是否必填 | 数据类型 |
|---|---|---|---|
| 创建时间 | cjsj | 否 | 字符串 |
### 2. 仓库管理员（gly）— 1项
| 数据类型 | 是否必填 | 属性名称 | 属性编号 |
|---|---|---|---|
| 字符串 | 否 | 创建时间 | cjsj |
'''
    with TestClient(create_app(tmp_path/'workspace',testing=True)) as client:
        def ok(resp):
            assert resp.status_code==200,resp.text
            return resp.json()
        pid=ok(client.post('/api/projects',json={'name':'测试列重排'}))['project']['id']
        r=ok(client.post(f'/api/projects/{pid}/documents',files={'file':('schema.md',src.encode(),'text/markdown')}))
        docs=ok(client.get(f'/api/projects/{pid}'))['documents']
        doc_id=docs[0]['id']
        ok(client.post(f'/api/projects/{pid}/analyze'))
        ok(client.put(f'/api/projects/{pid}/topic',json={'summary':'仓储本体','scope':'仓库与人员关系'}))
        identities={}
        for name in ('仓库','仓库管理员'):
            result=ok(client.post(f'/api/projects/{pid}/proposals',
                            json={'kind':'entity','payload':{'name':name,'kind':'class'}}))
            for prop_id in result['created']:
                ok(client.post(f'/api/projects/{pid}/proposals/{prop_id}/decision',json={'action':'accept'}))
        snap=ok(client.get(f'/api/projects/{pid}'))['snapshot']
        identities={e['name']:e['id'] for e in snap['entities']}
        ok(client.post(f'/api/projects/{pid}/stage',json={'target':'PROPERTY'}))
        rows=[('| 创建时间 | cjsj | 否 | 字符串 |','仓库'),
              ('| 字符串 | 否 | 创建时间 | cjsj |','仓库管理员')]
        for quote,name in rows:
            payload={'owner_id':identities[name],'name':'创建时间','datatype':'string',
                     'required':False,'multivalued':False}
            response=ok(client.post(f'/api/projects/{pid}/proposals',json={
                'kind':'attribute','payload':payload,
                'evidence':[{'document_id':doc_id,'locator':'伪造定位','quote':quote,
                             'source_code':'fake', 'source_name':'伪造属性'}]}))
            assert response['created'],response
            for prop_id in response['created']:
                ok(client.post(f'/api/projects/{pid}/proposals/{prop_id}/decision',json={'action':'accept'}))
        approved=ok(client.get(f'/api/projects/{pid}'))['snapshot']
        assert len(approved['attributes'])==2
        for a in approved['attributes']:
            evidence=json.loads(a['evidence'])
            assert evidence[0]['verified'] is True
            assert evidence[0]['source_code']=='cjsj'
            assert evidence[0]['source_name']=='创建时间'
            assert verified_source_attribute_code(a)=='cjsj'
        d='10000000-0000-4000-8000-000000000001';b='10000000-0000-4000-8000-000000000002'
        plan=make_plan(approved,pid,d,b)
        assert plan['ready'],plan['blockers']
        native=[step for step in plan['steps'] if step['kind']=='attribute']
        assert len(native)==1
        assert native[0]['data']['code']=='cjsj'
        assert len(native[0]['data']['entityIds'])==2
        assert all(uuid.UUID(x) for x in native[0]['data']['entityIds'])


def test_alias_headers_are_semantic_not_column_order_or_chapter_specific():
    source='''# 零售领域本体
## 47. 全量字段字典
| 适用对象 | 字段编码 | 字段类型 | 字段名称 |
|---|---|---|---|
| 门店、会员 | created_at | 日期时间 | 创建时间 |
## 97. 按实体挂载
### 97.1 门店（store）— 1项
| 字段名称 | 字段编码 | 字段类型 |
|---|---|---|
| 创建时间 | created_at | 日期时间 |
### 97.2 会员（member）— 1项
| 字段编码 | 字段类型 | 字段名称 |
|---|---|---|
| created_at | 日期时间 | 创建时间 |
'''
    chunks=markdown_chunks(source)
    cat=build_attribute_catalog([{'chunks':chunks}])
    assert cat['counts']['shared_attributes']==1,cat
    assert cat['counts']['direct_bindings']==2,cat
    assert cat['counts']['issues']==0,cat['issues']
    records=[deterministic_discover({'content':c['text'],'section':c['section'],'kind':c['kind']})
             for c in chunks if c['kind']=='table']
    attrs=[a for record in records for a in record['attributes']]
    assert {(a['owner'],a['name'],a['datatype']) for a in attrs}=={
        ('门店','创建时间','date'),('会员','创建时间','date')}
    assert sum(x['_coverage']['catalog_reference_rows'] for x in records)==1
    assert all(not record['_semantic_text'] for record in records)


def test_same_evidence_merges_stronger_verified_source_code_after_upgrade():
    from app.proposal_dedup import merge_evidence
    old=[{'document_id':'doc','locator':'行 12','quote':'| 创建时间 | cjsj |','verified':True}]
    new=[{'document_id':'doc','locator':'行 12','quote':'| 创建时间 | cjsj |','verified':True,
          'source_code':'cjsj','source_name':'创建时间'}]
    result=merge_evidence(old,new)
    assert len(result)==1 and result[0]['source_code']=='cjsj'
