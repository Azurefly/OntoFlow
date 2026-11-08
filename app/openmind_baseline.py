"""Read-only OpenMind branch baseline for incremental ontology construction.

The remote catalog is authoritative: retain all native fields verbatim (except
JSON serialization), and expose a compact schema context to the model. Only
Entity identity stubs are projected into the local editor so newly proposed
attributes and relationships may refer to *existing* OpenMind entity UUIDs.
The stubs are never written back as ontology changes.
"""
from __future__ import annotations
import hashlib
import json
import re
from collections import Counter
from .openmind import parse_catalog, uuid_value, OpenMindError

LABELS = {'entity': 'entities', 'attribute': 'attributes', 'relation': 'relations',
          'action': 'actions', 'capability': 'capabilities'}


def canonical_catalog(raw):
    source=raw.get('data') if isinstance(raw,dict) and isinstance(raw.get('data'),dict) and 'entities' not in raw else raw
    if isinstance(source,dict):
        for plural in LABELS.values():
            rows=source.get(plural,[])
            if not isinstance(rows,list):
                raise OpenMindError(f'OpenMind catalog.{plural} 必须为数组')
            seen=set()
            for row in rows:
                if not isinstance(row,dict) or not row.get('id'):
                    raise OpenMindError(f'OpenMind catalog.{plural} 包含无 UUID 的对象；禁止静默忽略')
                if str(row['id']) in seen:
                    raise OpenMindError(f'OpenMind catalog.{plural} 出现重复 UUID；无法建立可靠基线')
                seen.add(str(row['id']))
    indexed = parse_catalog(raw)
    canonical = {plural: sorted(indexed[kind].values(), key=lambda o: str(o['id']))
                 for kind, plural in LABELS.items()}
    for kind in LABELS:
        for obj in canonical[LABELS[kind]]:
            uuid_value(obj['id'], f'OpenMind {kind}.id')
            for scope_field in ('branchId','domainId'):
                if scope_field in obj:uuid_value(obj[scope_field],f'OpenMind {kind}.{scope_field}')
            if not isinstance(obj.get('name'), str) or not obj['name'].strip():
                raise OpenMindError(f'OpenMind {kind} 缺少对象名称，无法使用分支基线')
            if not isinstance(obj.get('code'), str) or not obj['code'].strip():
                raise OpenMindError(f'OpenMind {kind} 缺少业务 CODE，无法安全比对')
    # CODE is unique across ALL five object kinds in the target branch, not
    # merely within a single entity/attribute/relation array.
    used_codes={}
    used_ids={}
    for plural in LABELS.values():
        for obj in canonical[plural]:
            code=str(obj['code']).strip().casefold()
            if not re.fullmatch(r'[a-z][a-z0-9_]*',code) or len(code)>255:
                raise OpenMindError(f'OpenMind 对象 {obj["name"]} 存在无效 CODE：{obj["code"]}')
            if code in used_codes:
                raise OpenMindError(f'OpenMind 分支跨对象类型 CODE 冲突：{obj["code"]}（{used_codes[code]} / {plural}）')
            used_codes[code]=plural
            identity=str(obj['id'])
            if identity in used_ids:
                raise OpenMindError(f'OpenMind 分支跨对象类型 UUID 重复：{identity}')
            used_ids[identity]=plural
    # Current OpenMind contract only allows hard references within the same branch.
    entities={str(x['id']) for x in canonical['entities']}
    for obj in canonical['entities']:
        if obj.get('parentId') and str(obj['parentId']) not in entities:
            raise OpenMindError(f'OpenMind 实体 {obj["code"]} 的 parentId 不在分支内，拒绝不完整基线')
    for obj in canonical['attributes']:
        if any(str(eid) not in entities for eid in (obj.get('entityIds') or [])):
            raise OpenMindError(f'OpenMind 属性 {obj["code"]} 的 entityIds 引用了分支外实体')
    # Parent references must be acyclic. Each attribute may directly bind
    # unrelated entities, but not an ancestor together with its descendant.
    entity_index={str(x['id']):x for x in canonical['entities']}
    for entity in canonical['entities']:
        seen=set();cur=str(entity['id'])
        while cur in entity_index and entity_index[cur].get('parentId'):
            if cur in seen:
                raise OpenMindError(f'实体 {entity["name"]} 继承链存在环')
            seen.add(cur);cur=str(entity_index[cur]['parentId'])
    for obj in canonical['attributes']:
        direct={str(x) for x in obj.get('entityIds') or []}
        for owner in direct:
            seen=set();cur=owner
            while cur in entity_index and entity_index[cur].get('parentId'):
                cur=str(entity_index[cur]['parentId'])
                if cur in seen:break
                seen.add(cur)
                if cur in direct:
                    raise OpenMindError(f'共享属性 {obj["name"]} 同时绑定祖先和后代，违反直接属性继承约束')
    dependency_ids={'entity':entities,
                    'action':{str(x['id']) for x in canonical['actions']},
                    'capability':{str(x['id']) for x in canonical['capabilities']}}
    for obj in canonical['relations']:
        for dependency in obj.get('dependencies') or []:
            if not isinstance(dependency,dict) or dependency.get('kind') not in dependency_ids:
                raise OpenMindError(f'关系 {obj["code"]} 包含不支持的依赖类型')
            if str(dependency.get('id') or '') not in dependency_ids[dependency['kind']]:
                raise OpenMindError(f'关系 {obj["code"]} 依赖对象不在当前分支：{dependency["kind"]}')
    for obj in canonical['relations']:
        for field in ('sourceEntityIds','targetEntityIds'):
            endpoints=obj.get(field)
            if not isinstance(endpoints,list) or not endpoints:
                raise OpenMindError(f'OpenMind 关系 {obj["code"]} 缺少非空 {field}')
            if any(str(eid) not in entities for eid in endpoints):
                raise OpenMindError(f'OpenMind 关系 {obj["code"]} 的 {field} 引用了分支外实体')
    return canonical


def baseline_hash(canonical):
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':')).encode()).hexdigest()


def baseline_public(row):
    if not row:
        return None
    return {k: row[k] for k in ('domain_id', 'branch_id', 'fingerprint', 'refreshed_at',
                                'entity_count', 'attribute_count', 'relation_count',
                                'action_count', 'capability_count')}


def baseline_objects(row):
    return json.loads(row['catalog_json']) if row else None


def _tokens(text):
    text = str(text).casefold()
    words = set(re.findall(r'[a-z_][a-z0-9_]+|\d+', text))
    chars = re.findall(r'[\u3400-\u9fff]', text)
    words.update(chars)
    words.update(chars[i] + chars[i+1] for i in range(len(chars)-1))
    return words


def model_baseline_context(catalog, stage, source_text='', limit=13000):
    """Bounded *relevant* selection, not first-N truncation or a full-data promise.

    Returns the total count and explicit selected/omitted count per kind. A
    conservative lexical ranker is used until a private embedding index is
    configured. Exact entity IDs are retained so new proposals may reference
    existing types. The original full catalog remains queryable via API.
    """
    if not catalog:
        return None
    source_terms = _tokens(source_text)
    kinds = ('entities', 'attributes', 'relations')
    stage_focus = {'ENTITY': ('entities','relations','attributes'),
                   'PROPERTY': ('attributes','entities','relations'),
                   'RELATION': ('relations','entities','attributes')}.get(stage, kinds)
    rows = []
    for category in stage_focus:
        for obj in catalog.get(category, []):
            fields = {'id','name','code','description','parentId','entityIds','dataType',
                      'sourceEntityIds','targetEntityIds','executionMode','steps',
                      'primaryKeyAttributeIds','displayAttributeIds','symmetric', 'dependencies'}
            item = {k: (v[:280] if isinstance(v,str) and k in ('description','steps') else v)
                    for k,v in obj.items() if k in fields}
            score = len(source_terms & _tokens(' '.join(str(obj.get(k,'')) for k in ('name','code','description','steps'))))
            # Focus on defining the current stage; cross-stage context still present.
            score += 2 if category == stage_focus[0] else 0
            rows.append((score, category, item))
    rows.sort(key=lambda t:(-t[0], stage_focus.index(t[1]), str(t[2].get('code',''))))
    selected = {k:[] for k in kinds}
    remaining = max(1000, limit - 450)
    for _,category,obj in rows:
        encoded = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
        if len(encoded) + 4 > remaining:
            continue
        selected[category].append(obj)
        remaining -= len(encoded) + 4
    return {
        'source': 'OpenMind 指定分支已存在的权威对象（只读基线；不得假定未展示的对象不存在）',
        'totals': {k:len(catalog[k]) for k in kinds},
        'selected': {k:len(selected[k]) for k in kinds},
        'omitted': {k:len(catalog[k])-len(selected[k]) for k in kinds},
        'objects': selected,
        'policy': ('优先复用已有实体类型/属性/关系。仅对文档证明的新事实提出补充候选；'
                   '不要重复创建已存在定义；如需修改已有定义，提出人工澄清，禁止直接覆盖。'
                   '本体当前已存在的内容优先于文档推测。')
    }


def duplicate_against_baseline(kind, payload, catalog):
    """Prevent model silently recreating remote concepts, properties or edges."""
    if not catalog or payload.get('operation','add') != 'add':
        return ''
    entities = {str(e['id']):e for e in catalog['entities']}
    if kind == 'entity':
        if payload.get('kind','class')!='class':return ''
        name = str(payload.get('name','')).strip().casefold()
        existing = next((e for e in catalog['entities'] if e['name'].strip().casefold()==name), None)
        if existing:
            return f'该实体类型已在 OpenMind 分支存在（{existing["code"]} / {existing["id"]}）；应复用而非新增'
    elif kind == 'attribute':
        name = str(payload.get('name','')).strip().casefold()
        owner = str(payload.get('owner_id',''))
        # Include inherited attributes: child should not duplicate a parent's direct attribute.
        ancestors = {owner}
        while owner in entities and entities[owner].get('parentId') and entities[owner]['parentId'] not in ancestors:
            owner = str(entities[owner]['parentId']); ancestors.add(owner)
        for a in catalog['attributes']:
            if a['name'].strip().casefold()==name and ancestors.intersection(str(x) for x in (a.get('entityIds') or [])):
                if a.get('dataType')!=payload.get('datatype'):
                    raise OpenMindError(f'属性类型冲突：{a["name"]} 已有 {a.get("dataType")}，来源为 {payload.get("datatype")}；请人工选择保留或单独变更')
                return f'属性「{a["name"]}」已在指定实体或其父类中定义（{a["code"]} / {a["id"]}），应复用原属性'
    elif kind == 'relation':
        src = str(payload.get('source_id','')); tgt = str(payload.get('target_id',''))
        pred = re.sub(r'[^a-z0-9]', '', str(payload.get('predicate','')).casefold())
        for r in catalog['relations']:
            if (pred in (re.sub(r'[^a-z0-9]', '', str(r.get('code','')).casefold()), re.sub(r'[^a-z0-9]', '', str(r.get('name','')).casefold())) and
                src in [str(x) for x in r.get('sourceEntityIds',[])] and
                tgt in [str(x) for x in r.get('targetEntityIds',[]) ]):
                return f'OpenMind 已存在相同端点及名称的关系（{r["code"]} / {r["id"]}），应复用原关系'
    return ''
