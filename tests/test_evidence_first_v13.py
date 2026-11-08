"""Evidence-first extraction tests on synthetic Markdown ontology specifications.

The user-mentioned BC document is NOT available in this repository; these are
synthetic structural samples, not claims about its actual content.
"""
import json
from app import brain, incremental
from app.extraction import extract
from app.structured_discovery import markdown_chunks, deterministic_discover
from app.store import Store

MODEL={'base_url':'http://fake.invalid/v1','model':'stub','api_key':''}


def _piece(chunk, doc='d'):
    text=chunk['text']
    return {'content':text,'content_hash':incremental.sha(text),'document_id':doc,
            'locator':chunk['locator'],'kind':chunk.get('kind',''),
            'section':chunk.get('section','')}


def _example():
    return '''# 信息分发本体设计（合成测试样例）
## 3 实体定义
| 实体编号 | 实体名称 | 描述 |
|---|---|---|
| E-01 | 订阅申请 | 用户提交的请求 |
| E-02 | 订阅审核记录 | 审核过程记录 |

## 4 订阅审核记录的属性定义
| 字段名称 | 数据类型 | 描述 |
|---|---|---|
| 审核状态 | string | 审核结论 |
| 审核意见 | string | 审核人意见 |

## 5 关系定义
| 关系编号 | 源实体 | 关系名称 | 目标实体 |
|---|---|---|---|
| REL-01 | 订阅申请 | hasAudit | 订阅审核记录 |
'''


def test_md_preserves_complete_tables_headings_and_source_locations(tmp_path):
    f=tmp_path/'ontology.md';f.write_text(_example(),encoding='utf-8')
    result=extract(f)
    assert any(c['kind']=='heading' for c in result['chunks'])
    chunks=[c for c in result['chunks'] if c['kind']=='table']
    assert len(chunks)==3
    assert all(c['kind']=='table' and c['section'] and c['line_start']>0 for c in chunks)
    assert 'E-01' in chunks[0]['text'] and 'E-02' in chunks[0]['text']
    assert 'REL-01' in chunks[-1]['text']
    assert '行 ' in chunks[-1]['locator']


def test_known_tables_use_zero_llm_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *x:(_ for _ in ()).throw(AssertionError('unnecessary LLM call')))
    store=Store(tmp_path)
    chunks=markdown_chunks(_example())
    with brain.using_model(MODEL):
        outcomes=[incremental.cached_clues(store,_piece(c),MODEL,brain.joint_extract_segment) for c in chunks]
    assert all(not hit and result['_complete'] for result,hit in outcomes)
    assert [sum(len(result[k]) for result,_ in outcomes) for k in ('entities','attributes','relations')]==[2,2,1]
    # The second run must reuse every verified chunk.
    with brain.using_model(MODEL):
        assert all(incremental.cached_clues(store,_piece(c),MODEL,brain.joint_extract_segment)[1]
                   for c in chunks)


def test_attribute_owner_from_numbered_section():
    chunk=[c for c in markdown_chunks(_example()) if c['kind']=='table'][1]
    attr=deterministic_discover(_piece(chunk))['attributes'][0]
    assert attr['owner']=='订阅审核记录'
    assert attr['datatype']=='string' and attr['quote'] in chunk['text']


def test_explicit_heading_ent_number_is_a_grounded_entity():
    chunks=markdown_chunks('# 设计\n## 实体清单\n### E-17 审核记录\n\n它负责记录审批。')
    definitions=[deterministic_discover(_piece(c))['entities'] for c in chunks]
    assert [x['name'] for group in definitions for x in group]==['审核记录']
    assert 'E-17' in next(group[0]['quote'] for group in definitions if group)


def test_unknown_type_and_missing_owner_are_gaps_not_string_fallback():
    text='''## 字段表
| 属性名称 | 所属实体 | 数据类型 |
|---|---|---|
| 申请结果 | 订阅申请 | jsonb |
| 审批建议 | | integer |'''
    rows=[c for c in markdown_chunks(text) if c['kind']=='table'][0]
    clues=deterministic_discover(_piece(rows))
    assert clues['attributes'][0]['datatype']=='jsonb'
    assert len(clues['_unresolved'])==2
    current={'entities':[{'id':'1','name':'订阅申请','kind':'class'}], 'attributes':[],'relations':[]}
    _,gaps=incremental.align_clues('PROPERTY',clues,current,_piece(rows))
    assert any('五种单值类型' in gap['reason'] for gap in gaps)


def test_relation_direction_and_native_code_are_preserved():
    chunk=[c for c in markdown_chunks(_example()) if c['kind']=='table'][-1];clues=deterministic_discover(_piece(chunk))
    relation=clues['relations'][0]
    assert relation['source']=='订阅申请' and relation['target']=='订阅审核记录'
    assert relation['predicate']=='hasAudit'
    assert relation['code']=='REL-01'


def test_explicit_table_rows_not_sent_to_model_but_prose_is(monkeypatch):
    model_calls=[]
    def model(system,user):
        part=json.loads(user)['piece'];model_calls.append(part)
        assert 'E-01' not in part  # coded table already handled by rules
        return {'entities':[], 'attributes':[], 'relations':[{
            'source':'订阅申请','predicate':'relatedTo','target':'订阅审核记录',
            'quote':'订阅申请与订阅审核记录存在关联'}],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    chunks=markdown_chunks(_example()+'\n订阅申请与订阅审核记录存在关联。\n')
    with brain.using_model(MODEL):
        results=[brain.joint_extract_segment(_piece(c)) for c in chunks]
    assert len(model_calls)==1
    assert sum(len(r['relations']) for r in results)==2


def test_empty_model_result_cannot_prove_coverage(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *args:{'entities':[],'attributes':[],'relations':[],'has_more':False})
    p={'content':'订阅申请与审核记录之间存在关联，应建模为关系。','locator':'行 100','document_id':'d'}
    with brain.using_model(MODEL):result=brain.joint_extract_segment(p)
    assert not result['_complete']
    assert any('零条' in item['reason'] for item in result['_unresolved'])


def test_invented_model_quotes_are_rejected(monkeypatch):
    monkeypatch.setattr(brain,'query_llm',lambda *args:{'entities':[{'name':'虚构实体','quote':'原文没有这段话'}],
                   'attributes':[],'relations':[],'has_more':False})
    with brain.using_model(MODEL):result=brain.joint_extract_segment({'content':'审核记录可能与申请相关','locator':'L1'})
    assert result['entities']==[] and not result['_complete']
    assert any('无法在原文定位' in item['reason'] for item in result['_unresolved'])


def test_alias_and_code_resolve_native_baseline_identity_without_guessing():
    current={'entities':[],'attributes':[],'relations':[]}
    baseline={'entities':[{'id':'om-uuid-1','name':'订阅审核记录','code':'INFO_SUBSCRIBE_AUDIT','aliases':['订阅审核']}],
              'attributes':[],'relations':[]}
    piece={'document_id':'d','locator':'第1节','content':'属性：INFO_SUBSCRIBE_AUDIT.审核状态：string'}
    clues={'attributes':[{'owner':'INFO_SUBSCRIBE_AUDIT','name':'审核状态','datatype':'string',
                           'quote':piece['content']}], 'entities':[],'relations':[]}
    out,gaps=incremental.align_clues('PROPERTY',clues,current,piece,baseline=baseline)
    assert not gaps and out[0]['payload']['owner_id']=='om-uuid-1'


def test_ambiguous_alias_creates_question_instead_of_picking_first():
    current={'entities':[{'id':'one','name':'审核','kind':'class'},
                         {'id':'two','name':'审核','kind':'class'}], 'attributes':[],'relations':[]}
    piece={'document_id':'d','locator':'L1','content':'审核状态'}
    clues={'attributes':[{'owner':'审核','name':'审核状态','datatype':'string','quote':'审核状态'}]}
    out,gaps=incremental.align_clues('PROPERTY',clues,current,piece)
    assert out==[] and '歧义' in gaps[0]['reason']


def test_retrieval_reaches_end_chapter_not_just_first_9000_chars():
    doc={'id':'doc','filename':'large.md','chunks':json.dumps(
       [{'locator':f'第{i}章','text':('普通章节描述' * 90) if i<45 else 'REL-48 订阅与权限之间的关系定义'}
        for i in range(1,46)],ensure_ascii=False)}
    got=brain.retrieve_document_evidence([doc],'请解释 REL-48 的来源和目标',limit=1000)
    assert 'REL-48' in got['text'] and '第45章' in got['text']
    assert got['selected']>=1 and got['matching']>=1


def test_long_table_splits_by_rows_and_repeats_header_not_mid_cell():
    header='## 实体定义\n| 实体编号 | 实体名称 | 描述 |\n|---|---|---|\n'
    rows=[f'| E-{i:03d} | 对象{i} | 一种结构对象 |' for i in range(200)]
    chunks=[c for c in markdown_chunks(header+'\n'.join(rows),max_chars=1000) if c['kind']=='table']
    assert len(chunks)<20
    assert all(c['kind']=='table' for c in chunks)
    found=[e['name'] for c in chunks for e in deterministic_discover(_piece(c))['entities']]
    assert len(found)==200 and len(set(found))==200
    assert sum(len(c['text']) for c in chunks)<len(rows)*35+len(chunks)*120


def test_invalid_relation_predicate_requires_user_confirmation():
    p={'document_id':'d','locator':'L1','content':'| 申请 | 审核通过 | 审核记录 |'}
    current={'entities':[{'id':'a','name':'申请','kind':'class'},
                         {'id':'b','name':'审核记录','kind':'class'}]}
    clues={'relations':[{'source':'申请','target':'审核记录','predicate':'审核通过','quote':p['content']}]}
    out,gaps=incremental.align_clues('RELATION',clues,current,p)
    assert out==[] and '英文 CODE' in gaps[0]['reason']


def test_markdown_headings_remain_in_summary_but_not_llm_discovery(tmp_path):
    f=tmp_path/'report.md'
    f.write_text(_example(),encoding='utf-8')
    parsed=extract(f)
    assert '# 信息分发本体设计' in parsed['content']
    doc={'id':'d','project_id':'p','filename':'report.md','chunks':json.dumps(parsed['chunks'],ensure_ascii=False)}
    assert any('3 实体定义' in x for x in brain._source_outline([doc]))
    pieces=list(incremental.segments([doc]))
    assert len(pieces)==3 and all(p['kind']=='table' for p in pieces)


def test_tabular_csv_and_xlsx_keep_header_and_can_extract_without_llm(tmp_path):
    from openpyxl import Workbook
    csv=tmp_path/'table.csv'
    csv.write_text('实体编号,实体名称,描述\nE-01,订阅申请,申请表\nE-02,审核记录,审核表\n',encoding='utf-8')
    content=extract(csv)
    table=next(c for c in content['chunks'] if c['kind']=='table')
    assert '实体名称' in table['text']
    assert [e['name'] for e in deterministic_discover(_piece(table))['entities']]==['订阅申请','审核记录']
    wb=Workbook();sheet=wb.active
    sheet.append(['源实体','关系名称','目标实体'])
    sheet.append(['订阅申请','hasAudit','审核记录'])
    p=tmp_path/'relations.xlsx';wb.save(p)
    relation_chunk=next(c for c in extract(p)['chunks'] if c['kind']=='table')
    assert deterministic_discover(_piece(relation_chunk))['relations'][0]['predicate']=='hasAudit'


def test_partial_result_retains_rule_evidence_on_llm_failure(tmp_path,monkeypatch):
    store=Store(tmp_path)
    text='实体：审核记录\n审核记录可能与申请关联'
    p={'content':text,'content_hash':incremental.sha(text),'document_id':'d','locator':'L1'}
    def fail(*args):raise RuntimeError('test model disconnected')
    monkeypatch.setattr(brain,'query_llm',fail)
    with brain.using_model(MODEL):
        try:incremental.cached_clues(store,p,MODEL,brain.joint_extract_segment)
        except RuntimeError:pass
    key=incremental.clue_cache_key(p,MODEL)
    saved=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',(key,))
    assert saved is not None
    result=json.loads(saved['result'])
    assert result['entities'][0]['name']=='审核记录'
    assert result['_pending'] and not result['_complete']


def test_ambiguous_table_unknown_header_does_not_claim_full_coverage():
    content='| 业务编号 | 业务解释 |\n|---|---|\n| 1 | 订阅申请关联审核记录 |'
    p={'content':content,'kind':'table','section':'业务表'}
    result=deterministic_discover(p)
    assert not any(result[k] for k in ('entities','attributes','relations'))
    assert any('未知表格结构' in x['reason'] for x in result['_unresolved'])
