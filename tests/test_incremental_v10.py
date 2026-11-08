from app import incremental
from app.store import Store


def test_chunk_cache_is_shared_and_hash_invalidation(tmp_path):
    store=Store(tmp_path)
    calls=[]
    def extractor(piece):
        calls.append(piece['content'])
        return {'entities':[{'name':'网关','quote':piece['content']}], 'attributes':[], 'relations':[]}
    model={'base_url':'http://example.invalid/v1','model':'qwen'}
    first={'content':'实体：网关','content_hash':incremental.sha('实体：网关')}
    a,hit=incremental.cached_clues(store,first,model,extractor)
    b,hit2=incremental.cached_clues(store,first,model,extractor)
    assert hit is False and hit2 is True and calls==['实体：网关'] and a==b
    changed={'content':'实体：路由器','content_hash':incremental.sha('实体：路由器')}
    _,hit3=incremental.cached_clues(store,changed,model,extractor)
    assert not hit3 and len(calls)==2


def test_alignment_reuses_confirmed_entities_without_model_call():
    clues={'entities':[{'name':'网关','quote':'网关'}],
           'attributes':[{'owner':'网关','name':'设备编号','datatype':'string','quote':'设备编号'}],
           'relations':[{'source':'网关','target':'未知模块','predicate':'hasPart','quote':'关系'}]}
    piece={'document_id':'d1','locator':'第1节','content':'网关 设备编号 关系'}
    current={'entities':[{'id':'existing-openmind-id','name':'网关','kind':'class'}], 'attributes':[],'relations':[]}
    proposals,gaps=incremental.align_clues('PROPERTY',clues,current,piece)
    assert proposals[0]['payload']['owner_id']=='existing-openmind-id'
    _,gaps=incremental.align_clues('RELATION',clues,current,piece)
    assert len(gaps)==1 and '尚未确认' in gaps[0]['reason']
