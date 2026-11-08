"""v1.3.3 readable overview and non-invented ETA regression tests."""
import os
import pytest
from pathlib import Path

from app import brain
from app.extraction import extract
from app.progress_estimate import DiscoveryETA
from app.readable_overview import structured_overview
from app.structured_discovery import deterministic_discover


def test_overview_is_separate_from_parser_diagnostics(monkeypatch):
    source='''# 示例业务本体设计V0.1
## 4. 实体定义
| 实体名称 | 实体编号 | 实体级别 | 实体类型 |
|---|---|---|---|
| 自然人 | zr | 核心实体 | 主体类 |
| 设备 | sb | 核心实体 | 物品类 |
| 事件 | sj | 扩展实体 | 事件类 |
## 5. 属性定义
| 属性名称 | 属性编号 | 数据类型 | 挂载实体 |
|---|---|---|---|
| 事件时间 | sj_sj | 日期 | 事件 |
## 7. 实体关系
### 7.1 REL-01｜自然人 → 设备｜使用设备（1:N）
| 项 | 内容 |
|---|---|
| 实体A | 自然人 |
| 实体B | 设备 |
| 关系 | 使用设备 |
### 7.2 REL-02｜自然人 → 事件｜参与事件（1:N）
| 项 | 内容 |
|---|---|
| 实体A | 自然人 |
| 实体B | 事件 |
| 关系 | 参与事件 |
### 7.3 REL-03｜事件 → 设备｜涉及物品（N:M）
| 项 | 内容 |
|---|---|
| 实体A | 事件 |
| 实体B | 设备 |
| 关系 | 涉及物品 |
'''
    from app.structured_discovery import markdown_chunks
    document={'filename':'示例.md','chunks':markdown_chunks(source)}
    monkeypatch.setattr(brain, 'query_llm', lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('must not invoke LLM')))
    result=brain.analyze([document],baseline={'entities':[],'attributes':[],'relations':[]})
    assert result['coverage']['strategy']=='structured-evidence'
    assert result['coverage']['entity_definitions']==3
    assert result['coverage']['relation_definitions']==3
    assert '解析覆盖' not in result['summary']
    assert '【识别的章节】' not in result['summary']
    assert 'OpenMind' not in result['summary']
    assert len(result['summary']) < 1300


def test_relation_kv_table_has_exact_source_evidence_and_stable_code():
    from app.structured_discovery import markdown_chunks
    raw='''## 7. 实体关系
### 7.1 REL-01｜自然人 → 设备｜使用设备（1:N）
| 项 | 内容 |
|---|---|
| 实体A | 自然人 |
| 实体B | 设备 |
| 关系 | 使用设备 |
| 基数 | 1:N |
'''
    table=next(x for x in markdown_chunks(raw) if x['kind']=='table')
    found=deterministic_discover({'content':table['text'],'kind':'table','section':table['section']})
    assert found['relations'][0]['source']=='自然人'
    assert found['relations'][0]['target']=='设备'
    assert found['relations'][0]['predicate']=='rel_01'
    assert found['relations'][0]['quote'] in table['text']
    assert not found['_semantic_text'].strip()


def test_relation_conflict_requires_clarification():
    from app.structured_discovery import markdown_chunks
    raw='''## 7. 实体关系
### 7.1 REL-01｜自然人 → 设备｜使用设备（1:N）
| 项 | 内容 |
|---|---|
| 实体A | 其他人 |
| 实体B | 设备 |
| 关系 | 使用设备 |
'''
    table=next(x for x in markdown_chunks(raw) if x['kind']=='table')
    found=deterministic_discover({'content':table['text'],'kind':'table','section':table['section']})
    assert found['relations']==[]
    assert any('不一致' in gap['reason'] for gap in found['_unresolved'])


def test_ability_tables_not_sent_as_ontology_clues():
    from app.structured_discovery import markdown_chunks
    raw='''## 8. 原子能力说明
### 8.1 D01 业务能力
| 项 | 内容 |
|---|---|
| 能力 | 效果评估 |
| 关联实体 | 自然人 |
'''
    table=next(x for x in markdown_chunks(raw) if x['kind']=='table')
    found=deterministic_discover({'content':table['text'],'kind':'table','section':table['section']})
    assert not any(found[k] for k in ('entities','attributes','relations'))
    assert not found['_unresolved']
    assert not found['_semantic_text']


def test_eta_hides_early_and_volatile_results():
    eta=DiscoveryETA(parallelism=2)
    for _ in range(5):eta.observe(1,35,model_invoked=True)
    assert eta.estimate(5,150) is None
    for _ in range(20):eta.observe(1,35,model_invoked=True)
    assert eta.estimate(25,150) is not None
    eta.observe(1,800,model_invoked=True)
    assert eta.estimate(26,150) is None


def test_eta_does_not_extrapolate_cache_and_rules_as_llm():
    eta=DiscoveryETA(parallelism=4)
    for _ in range(40):eta.observe(1,0.01,model_invoked=False)
    for _ in range(6):eta.observe(1,12,model_invoked=True)
    estimate=eta.estimate(46,100)
    assert estimate is not None
    assert estimate < 100  # 54 remaining, only 6/46 required a model


def test_real_bc_document_if_available():
    """Runs against optional local evidence fixture; it is NEVER bundled in ZIP."""
    fixture=Path(os.getenv('ONTOFLOW_BC_FIXTURE','/mnt/data/信息分发本体设计 (BC 项目)-v0.1.md'))
    if not fixture.is_file():
        pytest.skip('Set ONTOFLOW_BC_FIXTURE to run the real-document test')
    parsed=extract(fixture)
    document={'filename':fixture.name,'chunks':parsed['chunks']}
    overview=structured_overview([document],{'entities':[],'attributes':[],'relations':[]})
    assert overview['coverage']['entity_definitions']==22
    assert overview['coverage']['attribute_definitions']==283
    assert overview['coverage']['relation_definitions']==48
    assert overview['coverage']['capability_domains']==12
    assert '一、业务与建模目标' in overview['summary']
    assert '【识别的章节】' not in overview['summary']
    count={'semantic':0,'relations':0,'unresolved':0}
    for part in parsed['chunks']:
        if part.get('kind')=='heading':continue
        clues=deterministic_discover({'content':part['text'],'kind':part.get('kind'),
                                    'section':part.get('section')})
        count['semantic']+=bool(clues['_semantic_text'].strip())
        count['relations']+=len(clues['relations'])
        count['unresolved']+=len(clues['_unresolved'])
    assert count['relations']==48
    assert count['semantic']==0
    assert count['unresolved']<=15
