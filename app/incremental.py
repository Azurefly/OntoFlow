"""Versioned, shared per-source-segment discovery cache.

The cache intentionally contains no OpenMind UUID or current review decisions.
It is not the accepted ontology. Changing the branch or the approvals only
re-runs alignment, not expensive document understanding.
"""
from __future__ import annotations
import hashlib
import json
import re
import threading
import os
from .ontology_abstraction import normalize_clues, abstract_reference, ABSTRACTION_VERSION
from .naming import relation_name
from .store import j,now
from .native_semantics import semantically_equal

PARSER_VERSION='source-chunk-v7-occurrence-v142'
EXTRACTOR_VERSION='evidence-first-v143-contract-'+ABSTRACTION_VERSION
# Striped single-process coordination prevents concurrent same-content requests.
# The PG result key remains the authority; different worker processes would need
# additional coordination before scaling beyond the current single-worker setup.
_CACHE_LOCKS=[threading.Lock() for _ in range(127)]


def sha(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()


def segments(documents):
    for d in documents:
        for index, chunk in enumerate(json.loads(d['chunks']) if isinstance(d['chunks'],str) else d['chunks']):
            text=chunk.get('text','').strip()
            if not text or chunk.get('kind')=='heading':continue
            locator=str(chunk.get('locator',''))
            yield {'document_id':d['id'],'project_id':d['project_id'],
                   'ordinal':index,'locator':locator,'content':text,
                   'kind':str(chunk.get('kind') or ''),'section':str(chunk.get('section') or ''),
                   **{k:chunk[k] for k in ('context_before','source_part','source_order','layout_warning','parser','source_spans','semantic_unit_version') if k in chunk},
                   'content_hash':sha(text),
                   'id':sha(d['id']+'\0'+str(index)+'\0'+locator+'\0'+text)[:40]}


def persist_segments(store,document):
    pieces=list(segments([document]))
    with store.tx() as c:
        c.execute('DELETE FROM document_segments WHERE document_id=?',(document['id'],))
        for piece in pieces:
            c.execute('''INSERT INTO document_segments
               (id,document_id,project_id,locator,ordinal,content_hash,content,parser_version,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
               (piece['id'],piece['document_id'],piece['project_id'],piece['locator'],
                piece['ordinal'],piece['content_hash'],piece['content'],PARSER_VERSION,now()))
    return pieces


def clue_cache_key(piece,model,version=EXTRACTOR_VERSION):
    from .source_understanding import source_key
    intent=model.get('interpretation') or {}
    # Neutral parsing remains shared. Semantic output varies by confirmed scope,
    # human decisions AND target branch; new contexts cannot reuse old answers.
    return sha('|'.join((version,model.get('base_url',''),model.get('model',''),
        model.get('prompt_version',''),model.get('structured_output') or os.getenv('LLM_STRUCTURED_OUTPUT','off'),
        source_key(piece),intent.get('fingerprint','default'))))


def cached_clue_status(store,piece,model):
    row=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',
                  (clue_cache_key(piece,model),))
    if not row:return 'absent'
    value=json.loads(row['result'])
    return 'incomplete' if value.get('_complete') is False else 'complete'


def cached_clues(store,piece,model,extractor,*,resume_incomplete=True,force_retry=False):
    """Serialize same-content extraction in this worker process."""
    key=clue_cache_key(piece,model)
    with _CACHE_LOCKS[int(key[:8],16)%len(_CACHE_LOCKS)]:
        return _cached_clues_locked(store,piece,model,extractor,
                                    resume_incomplete=resume_incomplete,key=key,force_retry=force_retry)


def _cached_clues_locked(store,piece,model,extractor,*,resume_incomplete,key,force_retry=False):
    """Persist each source-unit checkpoint; only completed output is a cache hit."""
    from inspect import signature
    row=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',(key,))
    saved=json.loads(row['result']) if row else None
    if force_retry and saved and saved.get('_complete') is False:
        saved=None  # explicit user authorization, never an automatic infinite retry
    # v1/v2 extraction caches cannot certify source coverage. New v3 cache
    # intentionally revalidates them rather than silently accepting omissions.
    if saved is not None and saved.get('_complete') is not False:
        from .metrics import count
        count('interpretation_cache_hits')
        return saved,True
    if saved is not None and not resume_incomplete:return saved,False
    def save(value):
        if not isinstance(value,dict) or not all(isinstance(value.get(k),list)
                                                  for k in ('entities','attributes','relations')):
            raise ValueError('联合抽取结果缺少三类候选数组')
        with store.tx() as c:
            c.execute("""INSERT INTO extraction_cache
                (cache_key,content_hash,model_name,extractor_version,result,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
                result=excluded.result,created_at=excluded.created_at""",
                (key,piece['content_hash'],model.get('model',''),EXTRACTOR_VERSION,j(value),now()))
    from .metrics import count
    from .source_understanding import using_source_cache
    count('interpretation_cache_misses')
    params=signature(extractor).parameters
    with using_source_cache(store):
        if 'checkpoint' in params and 'on_checkpoint' in params:
            result=extractor(piece,checkpoint=saved,on_checkpoint=save)
        else:result=extractor(piece)
    save(result)
    return result,False


def _name_key(value):
    return re.sub(r'[\s_\-（）()·.．：:]+','',str(value or '')).casefold()


def _entity_lookup(current, baseline=None):
    """Return indexed class identities; never silently pick one ambiguous name."""
    index={}
    def add(alias, identity):
        key=_name_key(alias)
        if not key:return
        index.setdefault(key,set()).add(str(identity))
    for entity in current.get('entities',[]):
        if entity.get('kind')!='class':continue
        add(entity.get('name'),entity['id'])
        for key in ('code','aliases','synonyms'):
            val=entity.get(key,[])
            for name in val if isinstance(val,list) else [val]:add(name,entity['id'])
    if baseline:
        for entity in baseline.get('entities',[]):
            eid=str(entity['id'])
            add(entity.get('name'),eid);add(entity.get('code'),eid)
            for key in ('aliases','synonyms'):
                values=entity.get(key,[])
                for name in values if isinstance(values,list) else [values]:add(name,eid)
    return index


def align_clues(stage, clues, current, piece, baseline=None, catalog_reference=None):
    """Evidence-checked alignment to confirmed classes and native OpenMind IDs.

    Unresolved, ambiguous and unsupported references remain explicit gaps.
    No guessed entity UUID, datatype fallback, or ungrounded quote is accepted.
    """
    entities=_entity_lookup(current,baseline)
    clues=normalize_clues(clues,str(piece.get('content','')))
    output=[];gaps=[]
    source_kind={'ENTITY':'entities','PROPERTY':'attributes','RELATION':'relations'}[stage]
    kind={'ENTITY':'entity','PROPERTY':'attribute','RELATION':'relation'}[stage]
    content=str(piece.get('content',''))
    indexed_baseline={k:{str(x['id']):x for x in (baseline or {}).get(k,[])}
                     for k in ('entities','attributes','relations')}
    for item in clues.get(source_kind,[]):
        if not isinstance(item,dict):continue
        quote=str(item.get('quote','')).strip()
        from .semantic_units import quote_locations
        evidence=quote_locations(piece,quote)
        def gap(reason):
            gaps.append({'kind':kind,'payload':item,'evidence':evidence,'reason':reason})
        if not evidence:
            gap('模型/规则线索无法在当前原文片段核验证据，未采纳');continue
        def find_identity(name):
            matched=entities.get(_name_key(abstract_reference(name)),set())
            return next(iter(matched)) if len(matched)==1 else None, len(matched)>1
        # A JSON export from the SAME branch is a *read-only copy* of existing
        # objects, not an instruction to create them anew. Compare source UUID
        # and source CODE before reusing; never reuse source UUID across branches.
        if piece.get('kind','').startswith('native-') and baseline:
            native_kind=piece['kind']
            src_id=str(item.get('source_uuid',''))
            source_branch=str(item.get('source_branch_id',''))
            refs={'native-entity':'entities','native-attribute':'attributes',
                  'native-relation':'relations'}.get(native_kind)
            remote=indexed_baseline.get(refs,{}).get(src_id) if refs else None
            same_branch=bool(remote and source_branch and
                             source_branch==str(remote.get('branchId') or ''))
            if same_branch:
                if native_kind=='native-entity':
                    if item.get('native_payload') and semantically_equal('entity',remote,item['native_payload']):
                        continue
                    gap('同 UUID 的来源实体与当前分支名称或 CODE 不一致，需人工核对');continue
                if native_kind=='native-attribute':
                    owner_id=str(item.get('source_owner_uuid') or '')
                    if item.get('native_payload') and semantically_equal('attribute',remote,item['native_payload']):
                        continue
                    gap('同 UUID 属性的 CODE/名称/类型或绑定实体与当前 OpenMind 分支不一致，需人工审核变更');continue
                if native_kind=='native-relation':
                    if item.get('native_payload') and semantically_equal('relation',remote,item['native_payload']):
                        continue
                    gap('同 UUID 关系的完整执行语义、依赖、端点或内容与当前分支不一致；请人工核对变更，禁止静默复用');continue
            if native_kind=='native-inheritance':
                entity=indexed_baseline['entities'].get(src_id)
                if (entity and source_branch==str(entity.get('branchId') or '') and
                    str(entity.get('parentId') or '')==str(item.get('parent_uuid') or '')):
                    continue
        if kind=='entity':
            name=str(item.get('name','')).strip()
            if not name:
                gap('实体名称缺失');continue
            # Personnel/自然人 may be intentionally different business classes.
            # It is a possible synonym, not a license to silently pick a UUID.
            if name=='自然人' and not entities.get(_name_key(name)) and entities.get(_name_key('人员')):
                gap('OpenMind/已确认本体已有「人员」类，请协商选择复用为自然人还是新增独立「自然人」类型；不得自动创建重复概念');continue
            payload={'name':name,'kind':'class','description':str(item.get('description',''))[:1000]}
        elif kind=='attribute':
            name=str(item.get('name','')).strip()
            owner,ambiguous=find_identity(item.get('owner'))
            if ambiguous:
                gap('所属实体存在同名/别名歧义，映射到多个已有实体；请人工选择');continue
            if not name or not owner:
                gap('属性所属实体尚未被审核或在现有分支中不存在');continue
            dtype=str(item.get('datatype','')).strip().lower()
            if catalog_reference and catalog_reference.get('definitions'):
                code=str(item.get('code','')).strip()
                definition=catalog_reference['definitions'].get(code)
                if not definition:
                    gap(f'属性 CODE「{code}」不在共享属性目录中；必须人工确认');continue
                if (definition['name']!=name or definition['datatype']!=dtype or
                    str(item.get('owner','')).strip() not in definition['expected_owners']):
                    gap('属性与共享目录中的名称、类型或挂载实体不一致；不生成候选，需人工核对');continue
            # The native OpenMind contract supports exactly five scalar types.
            if dtype not in ('string','integer','double','boolean','date'):
                gap(f'属性类型「{dtype or "未声明"}」不符合 OpenMind 五种单值类型；需人工确认，禁止默认改为 string');continue
            payload={'owner_id':owner,'name':name,'datatype':dtype,
                     'required':False,'multivalued':False,
                     'description':str(item.get('description',''))[:1000]}
        else:
            if piece.get('kind')=='native-relation' and (
                item.get('symmetric') is True or int(item.get('dependenciesCount') or 0)>0 or
                bool(item.get('hasExecutionSpec')) or
                item.get('executionMode') not in (None,'SEMANTIC')):
                gap('原生关系含 symmetric、dependencies 或确定性执行规范；当前本地关系模型不能无损承载，禁止跨分支静默降级为普通 SEMANTIC 关系。请人工核查并走原生变更流程。')
                continue
            source_id,src_amb=find_identity(item.get('source'))
            target_id,tgt_amb=find_identity(item.get('target'))
            if src_amb or tgt_amb:
                gap('关系源或目标名称存在歧义，请用户选择确切实体');continue
            if not source_id or not target_id:
                gap('关系源或目标实体尚未确认');continue
            predicate=str(item.get('predicate') or item.get('code') or '').strip()
            if not re.fullmatch(r'[A-Za-z][\w-]{0,60}',predicate):
                gap('关系标识不是可直接使用的英文 CODE；请人工确认名称与方向');continue
            payload={'source_id':source_id,'target_id':target_id,
                     'predicate':predicate,
                     'description':str(item.get('description',''))[:1000]}
        if kind=='relation':payload['name']=relation_name(item)
        source=str(item.get('code') or '').strip()
        if source:
            payload['code']=re.sub(r'[^a-z0-9_]+','_',source.lower())
        output.append({'kind':kind,'payload':payload,'evidence':evidence,
                       '_basis':item.get('_basis',{'kind':'inference','method':'legacy_extractor','rationale':'未显式分类的建议'})})
    return output,gaps
