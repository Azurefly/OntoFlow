"""Versioned JSON schema extraction, including OpenMind export ZIP and JSON.

Input payloads are untrusted. This is source inspection, not importing objects to
OpenMind: no remote writes, no instance data model, no source UUID reuse across
branches without an explicit reviewed mapping.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
import re
from pathlib import Path
from zipfile import ZipFile, BadZipFile

JSON_DISCOVERY_VERSION = 'json-schema-v2-complete'
NATIVE_TYPES=('entities','attributes','relations')
ALL_TYPES=NATIVE_TYPES+('actions','capabilities')
MAX_NATIVE_MEMBERS=10000

class JSONDiscoveryError(ValueError):pass

def compact(data):
    return json.dumps(data,ensure_ascii=False,separators=(',',':'),sort_keys=True)

def _summary_chunk(info):
    return {'kind':'json-overview','section':'JSON本体目录', 'locator':'JSON 本体目录',
            'text':compact(info)}

def _chunk(kind,section,locator,payload):
    return {'kind':kind,'section':section,'locator':locator,'text':compact(payload)}

def is_native(data):
    """Require real schema identity, not merely any JSON with 'entities' key."""
    if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in NATIVE_TYPES):return False
    if not ('schemaVersion' in data or 'domain' in data):return False
    objects=[v for k in NATIVE_TYPES for v in data[k]]
    return bool(objects and all(isinstance(x,dict) and all(x.get(k) for k in ('name','code','id')) for x in objects))

def native_chunks(data, filename='data.json'):
    """Generate auditable, bounded schema units from trusted *structure* only."""
    from .openmind_baseline import canonical_catalog
    from .openmind import OpenMindError
    if not is_native(data):raise JSONDiscoveryError('不是合法的 OpenMind 原生目录 JSON')
    try:
        catalog=canonical_catalog(data)  # same branch references and CODE checks
    except OpenMindError as e:
        raise JSONDiscoveryError('原生本体 JSON 未通过结构校验：'+str(e)) from e
    from .openmind import uuid_value
    domain=data.get('domain') or {}
    try:
        doc_domain=uuid_value(domain['id'],'source domain.id') if domain.get('id') else None
        doc_branch_id=uuid_value(domain['activeBranchId'],'source domain.activeBranchId') if domain.get('activeBranchId') else None
        scopes=set(); roots=set()
        for group in ALL_TYPES:
            for obj in catalog[group]:
                scopes.add(uuid_value(obj.get('branchId'),'source '+group+'.branchId'))
                roots.add(uuid_value(obj.get('domainId'),'source '+group+'.domainId'))
        if len(scopes)>1 or len(roots)>1:
            raise OpenMindError('来源目录必须有且只有一个领域和分支作用域')
        if doc_domain and roots and doc_domain not in roots:raise OpenMindError('来源 domain.id 与对象 domainId 不一致')
        if doc_branch_id and scopes and doc_branch_id not in scopes:raise OpenMindError('来源 activeBranchId 与对象 branchId 不一致')
    except OpenMindError as exc:
        raise JSONDiscoveryError('来源 UUID/作用域校验失败：'+str(exc)) from exc
    by_id={str(x['id']):x['name'] for x in catalog['entities']}
    branch_ids={str(obj.get('branchId')) for group in ALL_TYPES for obj in catalog[group] if obj.get('branchId')}
    if len(branch_ids)>1:
        raise JSONDiscoveryError('原生对象包含多个 branchId，不能混合到同一建模来源')
    branch=next(iter(branch_ids),'')
    doc_branch=str((data.get('domain') or {}).get('activeBranchId') or '')
    if branch and doc_branch and branch!=doc_branch:
        raise JSONDiscoveryError('领域 activeBranchId 与原生对象 branchId 不一致；需选择明确的导出分支')
    counts={k:len(catalog[k]) for k in ALL_TYPES}
    owners=sum(len(a.get('entityIds') or []) for a in catalog['attributes'])
    unbound=sum(not a.get('entityIds') for a in catalog['attributes'])
    ancestry=sum(bool(e.get('parentId')) for e in catalog['entities'])
    name=str((data.get('domain') or {}).get('name') or Path(filename).stem)
    overview={'format':'openmind-native','schemaVersion':data.get('schemaVersion'),
              'domain':name,'sourceBranchId':branch or doc_branch,
              'counts':counts,'directAttributeBindings':owners,
              'unboundAttributes':unbound,'inheritanceEdges':ancestry,
              'resourceBindings':len(data.get('resourceBindings') or []),
              'note':'原生本体定义仅作来源证据；资源绑定/动作/能力不作为实体、属性、关系候选直接写入。'}
    chunks=[_summary_chunk(overview)]
    for i,obj in enumerate(data['entities']):
        p=dict(obj);p['nativePayload']=dict(obj)
        chunks.append(_chunk('native-entity','OpenMind / entities',f'$.entities[{i}]',p))
        if obj.get('parentId'):
            if str(obj['parentId']) not in by_id:
                raise JSONDiscoveryError(f'实体 {obj["name"]} 的 parentId 无法解析')
            inheritance={'source':obj['name'],'target':by_id[str(obj['parentId'])],
                         'sourceId':obj['id'],'parentId':obj['parentId'],
                         'sourceBranchId':branch,'predicate':'subClassOf'}
            chunks.append(_chunk('native-inheritance','OpenMind / 类型继承',f'$.entities[{i}].parentId',inheritance))
    for i,obj in enumerate(data['attributes']):
        base=dict(obj);base['nativePayload']=dict(obj)
        owners=obj.get('entityIds') or []
        if not owners:
            chunks.append(_chunk('native-unbound-attribute','OpenMind / 未绑定属性',f'$.attributes[{i}]',base))
        for j,owner in enumerate(owners):
            if str(owner) not in by_id:raise JSONDiscoveryError('属性 entityIds 存在无效引用')
            chunks.append(_chunk('native-attribute','OpenMind / attributes',f'$.attributes[{i}].entityIds[{j}]',
                                 {**base,'owner':by_id[str(owner)],'ownerId':owner}))
    for i,obj in enumerate(data['relations']):
        # Do not automatically merge multi-endpoint relations into all pairs;
        # that would introduce semantic facts not present in the definition.
        src=obj.get('sourceEntityIds') or [];dst=obj.get('targetEntityIds') or []
        base=dict(obj);base['nativePayload']=dict(obj)
        base['sourceEntityIds']=src;base['targetEntityIds']=dst
        base['dependenciesCount']=len(obj.get('dependencies') or [])
        base['hasExecutionSpec']=bool(obj.get('executionSpec'))
        base['sourceNames']=[by_id[str(x)] for x in src]
        base['targetNames']=[by_id[str(x)] for x in dst]
        chunks.append(_chunk('native-relation','OpenMind / relations',f'$.relations[{i}]',base))
    return chunks

def _scalar_type(values):
    vals=[v for v in values if v is not None]
    if not vals:return None
    if any(isinstance(v,(dict,list)) for v in vals):return None
    ts=set(type(v) for v in vals)
    if ts=={bool}:return 'boolean'
    if ts<={int} and bool not in ts:return 'integer'
    if ts<={int,float} and bool not in ts:return 'double' if float in ts else 'integer'
    if ts=={str}:
        # Strings that look like dates should NOT be converted based on one value.
        return 'string'
    return None

def explicit_definitions(data):
    """Minimal, explicitly typed ontology JSON without native UUIDs."""
    if not isinstance(data,dict) or not isinstance(data.get('entities'),list):return None
    rows=data['entities']
    if not rows or not all(isinstance(x,dict) and isinstance(x.get('name'),str) for x in rows):return None
    # No claim that a list of people with names is ontology schema.
    if not all(('attributes' in x or 'properties' in x or 'kind' in x) for x in rows) and not (data.get('attributes') or data.get('properties') or data.get('relations')):return None
    chunks=[];known=set()
    for i,obj in enumerate(rows):
        name=obj['name'].strip()
        if not name or obj.get('kind','class')!='class':
            raise JSONDiscoveryError('显式本体实体必须是具有名称的 class 类型，不允许实例')
        known.add(name)
        chunks.append(_chunk('json-class','JSON / 显式实体类型',f'$.entities[{i}]',
            {'entityType':name,'code':obj.get('code',''),'description':obj.get('description','')}))
        attrs=obj.get('attributes',obj.get('properties',[]))
        if isinstance(attrs,dict):attrs=[{'name':k,**(v if isinstance(v,dict) else {'type':v})} for k,v in attrs.items()]
        if not isinstance(attrs,list):raise JSONDiscoveryError('实体 attributes/properties 必须为数组或字段映射')
        for j,a in enumerate(attrs):
            if not isinstance(a,dict) or not isinstance(a.get('name'),str):
                raise JSONDiscoveryError('属性定义缺少 name')
            raw=str(a.get('dataType',a.get('type',''))).lower()
            mapped={'str':'string','number':'double','float':'double','int':'integer','bool':'boolean',
                    'datetime':'date'}.get(raw,raw)
            if mapped not in ('string','integer','double','boolean','date'):
                raise JSONDiscoveryError(f'属性 {a["name"]} 数据类型 {raw} 不支持；不得擅自转换')
            chunks.append(_chunk('json-field','JSON / 显式属性',f'$.entities[{i}].attributes[{j}]',
                {'entityType':name,'field':a['name'],'dataType':mapped,'code':a.get('code','')}))
    aliases={}
    for entity in rows:
        for key in ('id','code','name'):
            if entity.get(key):
                aliases.setdefault(str(entity[key]),set()).add(entity['name'].strip())
    for field in ('attributes','properties'):
        top=data.get(field,[])
        if isinstance(top,dict):top=[{'name':k,**(v if isinstance(v,dict) else {'type':v})} for k,v in top.items()]
        if not isinstance(top,list):raise JSONDiscoveryError('顶层 '+field+' 必须为数组或定义映射')
        for i,attr in enumerate(top):
            if not isinstance(attr,dict) or not isinstance(attr.get('name'),str) or not attr['name'].strip():
                raise JSONDiscoveryError('顶层属性必须包含非空 name')
            raw=str(attr.get('dataType',attr.get('type',''))).lower()
            dtype={'str':'string','number':'double','float':'double','int':'integer','bool':'boolean','datetime':'date'}.get(raw,raw)
            if dtype not in ('string','integer','double','date','boolean'):
                raise JSONDiscoveryError('顶层属性数据类型不受支持：'+raw)
            owner_values=attr.get('entityIds',attr.get('owners',attr.get('owner',attr.get('entity',''))))
            if isinstance(owner_values,str):owner_values=[owner_values] if owner_values else []
            if not isinstance(owner_values,list) or not owner_values:
                raise JSONDiscoveryError('顶层属性 '+attr['name']+' 必须明确 owner/entityIds；未绑定属性请用原生 Attribute 格式')
            for j,owner in enumerate(owner_values):
                matches=aliases.get(str(owner),set())
                if len(matches)!=1:raise JSONDiscoveryError('顶层属性所属实体无效或歧义：'+str(owner))
                chunks.append(_chunk('json-field','JSON / 显式属性',f'$.{field}[{i}].owners[{j}]',
                    {'entityType':next(iter(matches)),'field':attr['name'],'dataType':dtype,'code':attr.get('code','')}))
    for i,r in enumerate(data.get('relations',[])):
        if not isinstance(r,dict):raise JSONDiscoveryError('relations 中存在非对象定义')
        src=r.get('source') or r.get('sourceEntity');dst=r.get('target') or r.get('targetEntity')
        code=r.get('code') or r.get('predicate')
        if not all(isinstance(v,str) and v.strip() for v in (src,dst,code)):
            raise JSONDiscoveryError('关系定义缺少 source/target/code，不能推断方向')
        if src not in known or dst not in known:
            raise JSONDiscoveryError('关系引用的实体类型不在当前显式实体目录中')
        chunks.append(_chunk('json-relation','JSON / 显式关系',f'$.relations[{i}]',
            {'source':src,'target':dst,'code':code,'description':r.get('description') or r.get('name') or code}))
    count=Counter(c['kind'] for c in chunks)
    chunks.insert(0,_summary_chunk({'format':'explicit-ontology-json','domain':data.get('name','JSON 本体'),
         'counts':{'entities':count['json-class'],'attributes':count['json-field'],
                   'relations':count['json-relation']},
         'note':'明确的类型/属性/关系定义；缺少 OpenMind 原生 ID，新增对象须使用目标分支审批及新 UUID。'}))
    return chunks


def generic_chunks(data, filename):
    """Generic record JSON: derive types from *collection names and keys*, not values.

    A record array supplies an evidenced collection type, while scalar fields
    become attribute definitions. Nested object structures/foreign keys are
    explicitly unresolved and are NEVER invented as typed edges.
    """
    chunks=[];unresolved=[];collections=[];narratives=[]
    narrative_key=re.compile(r'description|content|detail|narrative|incident|case.?text|案情|警情|描述|经过|内容|情况',re.I)
    if isinstance(data,dict):
        for key,rows in data.items():
            if isinstance(rows,list) and rows and all(isinstance(x,dict) for x in rows):
                collections.append((key,rows))
    elif isinstance(data,list) and data and all(isinstance(x,dict) for x in data):
        collections=[(Path(filename).stem,data)]
    if not collections and isinstance(data,dict):
        for field in ('实体类型','实体','类名','entityType','className'):
            value=data.get(field)
            if isinstance(value,str) and value.strip():
                chunks.append(_chunk('json-class','JSON / 显式类型定义',f'$.{field}',
                            {'entityType':value.strip(),'sourceField':field}))
                break
    def narratives_from(value,path='$',depth=0):
        if depth>64:raise JSONDiscoveryError('JSON 嵌套超过 64 层，无法完整处理；请拆分而不是采样')
        if isinstance(value,dict):
            for key,item in value.items():
                sub=path+'['+json.dumps(str(key),ensure_ascii=False)+']'
                if isinstance(item,str) and narrative_key.search(str(key)) and item.strip():
                    if len(narratives)>=20000:raise JSONDiscoveryError('JSON 叙述超过 20000 段，拒绝静默截断；请分文件上传')
                    narratives.append({'kind':'prose','section':'JSON / 自然语言描述','locator':sub,'text':item.strip()})
                elif isinstance(item,(list,dict)):narratives_from(item,sub,depth+1)
        elif isinstance(value,list):
            for index,item in enumerate(value):narratives_from(item,f'{path}[{index}]',depth+1)
    narratives_from(data)
    if not collections and not narratives and not chunks:
        raise JSONDiscoveryError('JSON 未提供原生本体定义、对象集合或可识别的业务描述；请提供 JSON Schema、对象数组或案情/警情描述字段。')
    for key,rows in collections:
        if not isinstance(key,str) or not key.strip() or key.startswith('_'):
            continue
        if len(rows)>MAX_NATIVE_MEMBERS:raise JSONDiscoveryError('单个对象集合超过安全解析上限')
        # The path is the declared type candidate. Do not treat record.name/
        # record.personName or any ID VALUE as a class instance candidate.
        chunks.append(_chunk('json-class','JSON / 业务集合',f'$.{key}',
                             {'entityType':key,'collection':key,'records':len(rows)}))
        fields={}
        for index,row in enumerate(rows):
            for field,value in row.items():
                fields.setdefault(field,[]).append(value)
        for field,values in fields.items():
            t=_scalar_type(values)
            if not t:
                unresolved.append({'collection':key,'field':field,'reason':'字段含对象、数组或冲突类型；需人工决定属性类型或独立实体关系'})
                continue
            chunks.append(_chunk('json-field','JSON / 集合字段',f'$.{key}[*].{field}',
                                 {'entityType':key,'field':field,'dataType':t,'sampleCount':len(values)}))
    if not chunks and not narratives:raise JSONDiscoveryError('JSON 未包含可归纳为本体的对象或描述')
    counts=Counter(c['kind'] for c in chunks)
    chunks.extend(narratives)
    chunks.insert(0,_summary_chunk({'format':'generic-record-json','domain':Path(filename).stem,
                'counts':{'entities':counts['json-class'],'attributes':counts['json-field'],'relations':0},
                'unresolved':unresolved, 'narrativeSegments':len(narratives),
                'narrativeCoverage':{'total':len(narratives),'emitted':len(narratives),'omitted':0},
                'note':'仅归纳数组名称与字段定义；实际记录值、姓名、手机号与 ID 不进入本体。跨字段关系需人工确认。'}))
    return chunks

def extract_json_obj(data,filename):
    if is_native(data):return native_chunks(data,filename)
    # Near-native malformed input must not fall into generic records silently.
    if isinstance(data,dict) and ('schemaVersion' in data or 'domain' in data) and all(k in data for k in NATIVE_TYPES):
        raise JSONDiscoveryError('发现 OpenMind 导出结构，但对象缺少 id/code/name 或目录结构不合法；拒绝降级为普通 JSON 文本')
    explicit=explicit_definitions(data)
    if explicit is not None:return explicit
    return generic_chunks(data,filename)

def extract_zip(path):
    """Accept a narrowly-scoped JSON archive, reject ZipSlip, bombs and junk."""
    try:
        with ZipFile(path) as archive:
            entries=archive.infolist()
            if len(entries)>20:raise JSONDiscoveryError('ZIP 文件数量超过限制（20 项）')
            for item in entries:
                name=item.filename
                if name.startswith('/') or '\\' in name or '..' in Path(name).parts:
                    raise JSONDiscoveryError('ZIP 包含非法路径')
                if item.flag_bits&1:raise JSONDiscoveryError('不支持加密 ZIP')
                if item.file_size>8_000_000 or (item.compress_size and item.file_size/item.compress_size>250):
                    raise JSONDiscoveryError('ZIP 内容大小或解压比超出安全限制')
                if (item.external_attr>>16)&0o170000==0o120000:
                    raise JSONDiscoveryError('ZIP 内不允许符号链接')
            members=[x for x in entries if not x.is_dir() and Path(x.filename).name=='data.json']
            if len(members)!=1:raise JSONDiscoveryError('ZIP 必须包含唯一的 data.json 原生本体导出文件')
            if any(x for x in entries if not x.is_dir() and Path(x.filename).name not in ('manifest.json','data.json')):
                raise JSONDiscoveryError('ZIP 中存在未知文件；只接受 OpenMind manifest.json 和 data.json')
            data=json.loads(archive.read(members[0]).decode('utf-8-sig'))
            if not is_native(data):raise JSONDiscoveryError('ZIP data.json 不是有效 OpenMind 原生本体目录')
            manifest=next((x for x in entries if not x.is_dir() and Path(x.filename).name=='manifest.json'),None)
            if manifest:
                manifest_data=json.loads(archive.read(manifest).decode('utf-8-sig'))
                if manifest_data.get('schemaVersion')!=data.get('schemaVersion'):
                    raise JSONDiscoveryError('manifest.json 与 data.json 的 schemaVersion 不一致')
            return native_chunks(data,'data.json')
    except (BadZipFile,UnicodeError,json.JSONDecodeError,KeyError,TypeError) as e:
        raise JSONDiscoveryError('ZIP 中原生 JSON 数据损坏或不是合法对象') from e

NATIVE_KINDS=frozenset(('native-entity','native-attribute','native-unbound-attribute',
                        'native-relation','native-inheritance','json-overview',
                        'json-class','json-field','json-relation'))

def discover_json_clues(piece):
    """Evidence-exact, LLM-free clues from already validated JSON units.

    Separate originating-branch identifiers are retained for audit, never used
    as a target branch identity without matching OpenMind baseline resolution.
    """
    kind=piece.get('kind','')
    text=str(piece.get('content',''))
    try:doc=json.loads(text)
    except (json.JSONDecodeError,TypeError) as e:
        raise JSONDiscoveryError('结构化 JSON 提取单元无法读取，请重新解析原始文件') from e
    output={'entities':[],'attributes':[],'relations':[], '_unresolved':[],
            '_semantic_text':'','_coverage':{'json_structural_units':1,'model_calls':0}}
    def note(reason,blocking=False):
        output['_unresolved'].append({'source_excerpt':text[:160],'reason':reason,
                                       'category':'json_schema','blocks_coverage':bool(blocking)})
    if kind in ('native-entity','json-class'):
        name=str(doc.get('name') if kind=='native-entity' else doc.get('entityType') or '')
        if name:
            output['entities'].append({'name':name,'code':doc.get('code',''),
                'source_uuid':doc.get('id',''), 'source_branch_id':doc.get('branchId',''),
                'source_parent_uuid':doc.get('parentId'), 'description':doc.get('description','') or '业务集合类型定义（来源 JSON 键名）。',
                'quote':json.dumps(name,ensure_ascii=False)[1:-1], 'native_payload':doc.get('nativePayload')})
    elif kind in ('native-attribute','json-field'):
        name=str(doc.get('name') if kind=='native-attribute' else doc.get('field') or '')
        if name:
            output['attributes'].append({'owner':doc.get('owner') if kind=='native-attribute' else doc.get('entityType'),
                'name':name,'code':doc.get('code',''),
                'datatype':doc.get('dataType',''),'description':doc.get('description','') or 'JSON 来源字段定义。',
                'source_uuid':doc.get('id',''),'source_branch_id':doc.get('branchId',''),
                'source_owner_uuid':doc.get('ownerId',''), 'quote':json.dumps(name,ensure_ascii=False)[1:-1], 'native_payload':doc.get('nativePayload')})
    elif kind=='native-inheritance':
        output['relations'].append({'source':doc['source'],'target':doc['target'],
            'predicate':'subClassOf','description':'源类型直接继承目标类型。',
            'source_uuid':doc.get('sourceId',''),'parent_uuid':doc.get('parentId',''),
            'source_branch_id':doc.get('sourceBranchId',''),'quote':json.dumps(doc['source'],ensure_ascii=False)[1:-1]})
    elif kind=='json-relation':
        output['relations'].append({'source':doc['source'],'target':doc['target'],
            'predicate':doc['code'],'description':doc.get('description') or doc['code'],
            'quote':json.dumps(doc['code'],ensure_ascii=False)[1:-1], 'native_payload':doc.get('nativePayload')})
    elif kind=='native-relation':
        src=doc.get('sourceNames') or [];dst=doc.get('targetNames') or []
        if len(src)!=1 or len(dst)!=1:
            note('原生关系有多个源或目标实体：当前审核器仅支持一对类型端点；必须保留原生多端点结构并人工决定扩展方式，不能自动拆成多条关系。')
        else:
            # Name is not CODE. Preserve native code for provenance and later
            # exact, checked mapping; do not substitute generated predicate.
            output['relations'].append({'source':src[0],'target':dst[0],
                'predicate':doc['code'],'code':doc['code'],
                'label':doc.get('name',''),'description':doc.get('steps') or doc.get('description') or doc['name'],
                'native_steps':doc.get('steps') or '',
                'source_uuid':doc.get('id',''),'source_branch_id':doc.get('branchId',''),
                'executionMode':doc.get('executionMode'),'symmetric':doc.get('symmetric'),
                'dependenciesCount':doc.get('dependenciesCount',0),
                'hasExecutionSpec':doc.get('hasExecutionSpec',False),
                'quote':json.dumps(doc['code'],ensure_ascii=False)[1:-1], 'native_payload':doc.get('nativePayload')})
    elif kind=='native-unbound-attribute':
        note('原生属性「'+str(doc.get('name',''))+'」未绑定任何实体；当前 OntoFlow 的属性审核依赖所属实体。此项独立定义必须显式保留待协商，不能遗漏或猜测挂载。')
    elif kind=='json-overview':
        for item in (doc.get('unresolved') or []):
            if isinstance(item,dict):note('业务 JSON：'+str(item.get('collection',''))+'.'+str(item.get('field',''))+'：'+str(item.get('reason','')))
    return output
