"""Regression for v1.2.4's eight-page has_more failure and lost work."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from app import brain,incremental
from app.store import Store

CONFIG={'base_url':'http://model.invalid/v1','model':'stub','api_key':''}


def piece(text):
    return {'content':text,'content_hash':incremental.sha(text),'document_id':'doc','locator':'L1'}


def test_false_has_more_is_not_required_for_success(tmp_path,monkeypatch):
    store=Store(tmp_path)
    calls=[]
    def model(system,user):
        calls.append(json.loads(user)['piece'])
        return {'entities':[{'name':'网关','quote':'网关'}], 'attributes':[],
                'relations':[], 'has_more':False}
    monkeypatch.setattr(brain,'query_llm',model)
    p=piece('实体：网关')
    with brain.using_model(CONFIG):
        result,hit=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
        repeated,hit2=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
    assert not hit and hit2 and len(calls)==0  # explicit definition uses rules; no LLM
    assert result['_complete'] is True and repeated==result
    assert result['entities'][0]['quote']=='实体：网关'


def test_bounded_has_more_splits_source_and_resumes_without_restart(tmp_path,monkeypatch):
    store=Store(tmp_path)
    calls=[]
    def model(system,user):
        data=json.loads(user); text=data['piece'];calls.append(text)
        return {'entities':[{'name':'同一实体','description':str(len(calls)),'quote':'甲'}],
                'attributes':[], 'relations':[], 'has_more':len(text)>250}
    monkeypatch.setattr(brain,'query_llm',model)
    monkeypatch.setenv('JOINT_EXTRACT_CALL_BUDGET','4')
    p=piece('甲'*1000)
    with brain.using_model(CONFIG):
        result,hit=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
        assert not hit and result['_complete'] is False
        assert incremental.cached_clue_status(store,p,CONFIG)=='incomplete'
        assert len(result['_pending'])>0
        first_pending=result['_pending'][0]['text']
        checkpoint,_=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment,
                                                resume_incomplete=False)
        assert checkpoint==result and len(calls)==4
        finished,hit2=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
        assert not hit2 and finished['_complete'] is True
        _,hit3=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
    assert hit3 and incremental.cached_clue_status(store,p,CONFIG)=='complete'
    assert len(calls)==7 and calls[4]==first_pending
    assert len(finished['entities'])==1  # identity-based de-dup, not JSON equality
    assert finished['entities'][0]['quote']=='甲'  # grounded source quotation


def test_model_never_stops_does_not_claim_complete(tmp_path,monkeypatch):
    store=Store(tmp_path)
    calls=[]
    def model(system,user):
        calls.append(1)
        return {'entities':[{'name':'候选','quote':'不存在'}],
                'attributes':[], 'relations':[], 'has_more':True}
    monkeypatch.setattr(brain,'query_llm',model)
    p=piece('订阅申请与订阅审核记录之间存在关联')
    with brain.using_model(CONFIG):
        result,_=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
        reloaded,hit=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
    assert len(calls)==1 and not hit and not result['_complete']
    assert result['_unresolved'] and not result['_pending']
    assert reloaded==result  # never mislabel as successful cache hit
    assert len(result['entities'])==0  # invented quote cannot become a valid clue


def test_model_error_leaves_last_good_checkpoint(tmp_path,monkeypatch):
    store=Store(tmp_path)
    calls=[]
    def failing(system,user):
        text=json.loads(user)['piece'];calls.append(text)
        if len(calls)==2:raise RuntimeError('模型断开连接')
        return {'entities':[],
                'attributes':[],'relations':[],'has_more':True}
    monkeypatch.setattr(brain,'query_llm',failing)
    p=piece('B'*600)
    with brain.using_model(CONFIG):
        try:
            incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
            assert False,'Expected second call to fail'
        except RuntimeError:pass
    assert len(calls)==2
    checkpoint=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',
                         (incremental.clue_cache_key(p,CONFIG),))
    assert checkpoint and json.loads(checkpoint['result'])['_pending'][0]['text']!='B'*600
    # Resume from a subdivided unit rather than paying for the full chunk again.
    def recovered(system,user):
        calls.append(json.loads(user)['piece'])
        return {'entities':[],'attributes':[],'relations':[],'has_more':False}
    monkeypatch.setattr(brain,'query_llm',recovered)
    with brain.using_model(CONFIG):
        final,hit=incremental.cached_clues(store,p,CONFIG,brain.joint_extract_segment)
    assert not hit and final['_complete'] is True
    assert calls[2]!='B'*600


def test_same_content_parallel_calls_are_deduplicated(tmp_path):
    store=Store(tmp_path)
    lock=threading.Lock();calls=[]
    def extractor(p):
        with lock:calls.append(1)
        time.sleep(.03)
        return {'entities':[],'attributes':[],'relations':[]}
    p=piece('共同缓存')
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:incremental.cached_clues(store,p,CONFIG,extractor),range(5)))
    assert len(calls)==1
    assert sorted(hit for _,hit in results)==[False,True,True,True,True]


def test_v1_cache_requires_new_evidence_validation(tmp_path):
    """Old complete caches have no coverage proof; v3 must revalidate."""
    from app.store import j,now
    store=Store(tmp_path)
    p=piece('旧版已确认完成的片段')
    historical={'entities':[{'name':'网关'}],'attributes':[],'relations':[]}
    oldkey=incremental.clue_cache_key(p,CONFIG,'joint-clues-v1')
    with store.tx() as c:
        c.execute('''INSERT INTO extraction_cache
           (cache_key,content_hash,model_name,extractor_version,result,created_at)
           VALUES(?,?,?,?,?,?)''',
           (oldkey,p['content_hash'],'stub','joint-clues-v1',j(historical),now()))
    assert incremental.cached_clue_status(store,p,CONFIG)=='absent'
    new={'entities':[],'attributes':[],'relations':[],'_complete':True}
    result,hit=incremental.cached_clues(store,p,CONFIG,lambda _:new)
    assert not hit and result==new
