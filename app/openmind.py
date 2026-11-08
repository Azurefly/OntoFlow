"""Strict, human-reviewed OntoFlow -> OpenMind native definition adapter.

Based on 2026-11-02 OpenMind result/writing contract. No direct DB writes.
"""
from __future__ import annotations
import hashlib, json, os, re, uuid
from urllib.parse import urlsplit
import httpx
from .openmind_transport_policy import enforce_transport_policy
from .demo_mode import enabled as demo_enabled
from .schema_catalog import verified_source_attribute_code, verified_source_object_code
from collections import defaultdict
from .native_semantics import prepare_write
from .naming import generated_code,relation_name,is_chinese_name,code_error

KINDS = ('entity','attribute','relation')
TYPES = {'integer','double','string','date','boolean'}
CODE = re.compile(r'^[a-z][a-z0-9_]*$')
class OpenMindError(ValueError): pass

def validate_base_url(raw, allowed_hosts=None):
    url=raw.strip().rstrip('/')
    try:p=urlsplit(url)
    except ValueError as exc:raise OpenMindError('OpenMind 服务地址格式无效') from exc
    if not url or p.scheme not in ('http','https') or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
        raise OpenMindError('OPENMIND_BASE_URL 必须是不带密钥、查询参数的 http/https 地址')
    allowed=set()
    if not demo_enabled():
        allowed={x.strip().lower() for x in (allowed_hosts if allowed_hosts is not None else os.getenv('OPENMIND_ALLOWED_HOSTS','').split(',')) if x.strip()}
        if allowed and p.hostname.lower() not in allowed: raise OpenMindError('OpenMind 地址不在白名单内')
    if p.path not in ('','/'):raise OpenMindError('OpenMind Base URL 应为服务器根地址，路径由适配层拼接')
    try: port=p.port
    except ValueError as exc:raise OpenMindError('OpenMind 端口无效') from exc
    if port is not None and not 1 <= port <= 65535:raise OpenMindError('OpenMind 端口无效')
    enforce_transport_policy(url, allowed)
    return url

def uuid_value(value, label):
    try: return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError): raise OpenMindError(f'{label} 必须为 UUID')

def native_id(pid, kind, local_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'ontoflow:openmind:{pid}:{kind}:{local_id}'))

def code_for(kind,local_id,name):
    return generated_code(kind,local_id,name)


def parse_catalog(raw):
    if isinstance(raw,dict) and 'entities' not in raw and isinstance(raw.get('data'),dict): raw=raw['data']
    if not isinstance(raw,dict) or not any(k in raw for k in ('entities','attributes','relations','actions','capabilities')):
        raise OpenMindError('OpenMind catalog 未返回可识别对象数组')
    kinds={}
    for plural,kind in [('entities','entity'),('attributes','attribute'),('relations','relation'),('actions','action'),('capabilities','capability')]:
        rows=raw.get(plural,[])
        if not isinstance(rows,list):raise OpenMindError(f'catalog.{plural} 必须为数组')
        kinds[kind]={str(x['id']):x for x in rows if isinstance(x,dict) and x.get('id')}
    return kinds

def make_plan(snap, pid, domain, branch, mappings=None, overrides=None, catalog=None):
    """Preview only. Fail closed for semantic loss and unknown target collisions."""
    domain=uuid_value(domain,'domainId'); branch=uuid_value(branch,'branchId')
    mappings=mappings or {}; overrides=overrides or {}; blockers=[]; warnings=[]; steps=[]
    target_index=parse_catalog(catalog) if catalog is not None else {}
    es={e['id']:e for e in snap['entities']}; classes={k:e for k,e in es.items() if e['kind']=='class'}
    # OpenMind baseline entities are reference anchors, NEVER new write steps.
    # Their native UUID is preserved, unlike local user-approved additions.
    remote_class_ids={k for k,e in classes.items() if e.get('origin')=='openmind'}
    attrs={a['id']:a for a in snap['attributes']}; rels={r['id']:r for r in snap['relations']}
    # The editor stores one accepted owner-attribute link per row. OpenMind is
    # different: Attribute is a shared definition with entityIds[] of directly
    # bound types. Merge ONLY when signed-off source evidence carries exactly
    # the same BC attribute CODE and compatible name/data type. No evidence
    # means separate native attributes; never group by display name alone.
    groups=defaultdict(list)
    for aid,a in attrs.items():
        source_code=verified_source_attribute_code(a)
        key=('catalog',source_code) if source_code else ('local',aid)
        groups[key].append(aid)
    ids={}; codes={}
    group_identity={}
    for key,aids in groups.items():
        aids=sorted(aids)
        variants={(attrs[aid]['name'],attrs[aid]['datatype']) for aid in aids}
        if len(variants)>1:
            blockers.append(f'共享属性 CODE {key[1]} 对应多个名称或数据类型；请人工核对，禁止合并')
        mapped_ids={mappings.get(('attribute',aid),{}).get('remote_id') for aid in aids if mappings.get(('attribute',aid),{}).get('remote_id')}
        explicit_codes={v for aid in aids for v in [overrides.get(('attribute',aid)) or mappings.get(('attribute',aid),{}).get('code') or attrs[aid].get('code')] if v}
        if len(mapped_ids)>1:blockers.append(f'共享属性 {key[1]} 已映射到多个 OpenMind UUID，禁止自动合并')
        if len(explicit_codes)>1:blockers.append(f'共享属性 {key[1]} 已设置不同 CODE，禁止自动合并')
        name=attrs[aids[0]]['name']
        generated=(re.sub(r'[^a-z0-9_]+','_',key[1]) if key[0]=='catalog' else code_for('attribute',aids[0],name))
        remote_id=(next(iter(mapped_ids)) if mapped_ids else native_id(pid,'attribute',
                   'catalog:'+key[1] if key[0]=='catalog' else aids[0]))
        native_code=(next(iter(explicit_codes)) if explicit_codes else generated)
        # A verified shared CODE identifies an existing native Attribute,
        # even when the newly approved local binding has never been mapped.
        matches=[x for x in target_index.get('attribute',{}).values()
                 if str(x.get('code','')).casefold()==native_code.casefold()]
        if matches and not mapped_ids:
            if len(matches)!=1 or matches[0].get('name')!=name or matches[0].get('dataType')!=attrs[aids[0]]['datatype']:
                blockers.append(f'共享属性 {native_code} 与目标名称/类型冲突，需单独变更审批')
            elif key[0]=='catalog':remote_id=matches[0]['id']
        group_identity[key]=(remote_id,native_code)
    for kind,items in [('entity',classes),('attribute',attrs),('relation',rels)]:
        for local_id,item in items.items():
            key=(kind,local_id); mapping=mappings.get(key,{})
            if kind=='attribute':
                group_key=('catalog',verified_source_attribute_code(item)) if verified_source_attribute_code(item) else ('local',local_id)
                ids[key],code=group_identity[group_key]
            else:
                ids[key]=local_id if kind=='entity' and local_id in remote_class_ids else (mapping.get('remote_id') or native_id(pid,kind,local_id))
                code=overrides.get(key) or mapping.get('code') or item.get('code') or verified_source_object_code(item,kind) or code_for(kind,local_id,item.get('name',item.get('predicate','')))
            if not CODE.fullmatch(code) or len(code)>(64 if kind=='relation' else 255):blockers.append(f'{kind}/{local_id}: CODE 不合法 {code}')
            codes[key]=code
    for (kind,lid),saved in mappings.items():
        items={'entity':classes,'attribute':attrs,'relation':rels}.get(kind,{})
        if lid not in items:
            blockers.append(f'此前已同步的 {kind}/{lid} 已从本地草稿移除；不能静默保留远端，需显式删除评审流程')
    for e in es.values():
        if e['kind']=='instance':blockers.append(f'具体实例 {e["name"]} 不是 OpenMind 的实体类型定义，需单独建模实例')
    parents={}
    for r in rels.values():
        if r['predicate']!='subClassOf': continue
        child,parent=r['source_id'],r['target_id']
        if child not in classes or parent not in classes:blockers.append('subClassOf 两端必须是实体类')
        if child in parents and parents[child]!=parent:blockers.append('OpenMind Entity 只能单继承')
        parents[child]=parent
    for child in parents:
        found=set();cur=child
        while cur in parents:
            if cur in found: blockers.append('继承结构存在环');break
            found.add(cur);cur=parents[cur]
    ordered=[];visited=set()
    def visit(eid,stack=frozenset()):
        if eid in visited or eid in stack:return
        parent=parents.get(eid)
        if parent in classes:visit(parent,stack|{eid})
        visited.add(eid);ordered.append(eid)
    for eid in sorted(classes,key=lambda k:classes[k]['name']):visit(eid)
    def common(kind,lid,name,description):
        if not is_chinese_name(name):blockers.append(f'{kind}/{lid} 缺少中文名称，请先审核补充；不能用 CODE 代替名称')
        if code_error(kind,codes[(kind,lid)]):blockers.append(f'{kind}/{lid}: '+code_error(kind,codes[(kind,lid)]))
        return dict(id=ids[(kind,lid)],domainId=domain,branchId=branch,name=name,code=codes[(kind,lid)],description=description or '',status='enabled')
    for eid in ordered:
        if eid in remote_class_ids:continue
        e=classes[eid]; data=common('entity',eid,e['name'],e['description']);parent=parents.get(eid)
        data.update(parentId=ids.get(('entity',parent)),primaryKeyAttributeIds=[],displayAttributeIds=[],attributeAliases={},fetchLogic='',remarks=[],favorite=False)
        steps.append(dict(kind='entity',local_id=eid,action='create',data=data))
    for group_key,aids in sorted(groups.items(), key=lambda kv:kv[0]):
        aids=sorted(aids)
        a=attrs[aids[0]]
        if len({(attrs[aid]['name'],attrs[aid]['datatype']) for aid in aids})>1:
            continue  # The explicit blocker above makes ready=False.
        owners=sorted({attrs[aid]['owner_id'] for aid in aids})
        invalid=[owner for owner in owners if owner not in classes]
        if invalid:
            blockers.append(f'共享属性 {a["name"]} 的部分所属实体不是有效本体类：{invalid}');continue
        if a['datatype'] not in TYPES:
            blockers.append(f'属性 {a["name"]} 的数据类型 {a["datatype"]} 不受支持，禁止静默转换');continue
        if any(attrs[aid]['multivalued'] for aid in aids):
            blockers.append(f'属性 {a["name"]} 是多值属性，原生接口仅支持单值');continue
        if any(attrs[aid]['required'] for aid in aids):
            warnings.append(f'属性 {a["name"]} 的 required 不是 OpenMind 原生字段，不会在目标系统生效')
        for owner in owners:
            parent=parents.get(owner)
            checked=set()
            while parent in classes and parent not in checked:
                if parent in owners:
                    blockers.append(f'共享属性 {a["name"]} 同时直接绑定父类及其子类，OpenMind 不允许继承重复属性')
                checked.add(parent)
                parent=parents.get(parent)
        data=common('attribute',aids[0],a['name'],a['description'])
        data.update(dataType=a['datatype'],entityIds=[ids[('entity',owner)] for owner in owners])
        steps.append(dict(kind='attribute',local_id=aids[0],local_ids=aids,
                          action='create',data=data))
    warnings.append('当前 OntoFlow 无实体主键、展示属性配置；不会猜测 primaryKeyAttributeIds / displayAttributeIds')
    for rid,r in sorted(rels.items(),key=lambda kv:kv[1]['predicate']):
        if r['predicate']=='subClassOf':continue
        if r['source_id'] not in classes or r['target_id'] not in classes:blockers.append(f'关系 {r["predicate"]} 的两端必须是实体类');continue
        desc=r['description'].strip()
        if not desc:blockers.append(f'关系 {r["predicate"]} 缺少描述/成立依据，不能生成 SEMANTIC.steps');continue
        data=common('relation',rid,relation_name(r),desc)
        data.update(sourceEntityIds=[ids[('entity',r['source_id'])]],targetEntityIds=[ids[('entity',r['target_id'])]],symmetric=False,associationMode='entity',dependencies=[],executionMode='SEMANTIC',steps=desc)
        warnings.append(f'关系 {r["predicate"]} 是语义模式，请人工审核 steps 是否足以作为业务判定依据，保存并不代表可执行')
        steps.append(dict(kind='relation',local_id=rid,action='create',data=data))
    if catalog is not None:
        index=parse_catalog(catalog); occupied={}
        for kind,objects in index.items():
            for remote in objects.values():
                if remote.get('code'):occupied.setdefault(str(remote['code']).casefold(),[]).append((kind,str(remote['id'])))
        for step in steps:
            kind,lid=step['kind'],step['local_id']; data=step['data']
            existing=index[kind].get(data['id'])
            if mappings.get((kind,lid)) and not existing:blockers.append(f'已映射对象 {data["code"]} 在目标分支不存在，不可自动重建')
            if kind=='attribute' and not existing:
                similar=[x for x in index['attribute'].values() if
                         str(x.get('name','')).strip().casefold()==str(data['name']).strip().casefold() and
                         str(x.get('dataType','')).casefold()==str(data.get('dataType','')).casefold()]
                if similar:
                    blockers.append(f'OpenMind 已有同名同类型共享属性 {data["name"]}；新增绑定应经人工审核复用旧 UUID，禁止另建同义属性')
            if existing:
                if str(existing.get('code','')).casefold()!=data['code'].casefold():blockers.append(f'同 UUID 对象 {data["code"]} 的目标 CODE 不一致')
                else:
                    if kind=='attribute':
                        if existing.get('dataType')!=data.get('dataType') or existing.get('name')!=data.get('name'):
                            blockers.append(f'共享属性 {data["code"]} 名称或类型变化，不能按新增挂载执行')
                        old_owners=set(existing.get('entityIds',[])); requested=set(data['entityIds'])
                        data['entityIds']=sorted(old_owners|requested)
                        step['approved_binding_additions']=sorted(requested-old_owners)
                        # Check ancestor ownership over both remote and new entities.
                        parent_map={str(x['id']):x.get('parentId') for x in index['entity'].values()}
                        for cid,par in parents.items():parent_map[ids[('entity',cid)]]=ids.get(('entity',par))
                        for owner in data['entityIds']:
                            node=parent_map.get(owner); chain=set()
                            while node and node not in chain:
                                if node in data['entityIds']:blockers.append(f'共享属性 {data["code"]} 的增量绑定与继承重复')
                                chain.add(node);node=parent_map.get(node)
                    if kind=='relation' and (existing.get('dependencies') or existing.get('executionSpec') or existing.get('symmetric')):
                        blockers.append(f'已有关系 {data["code"]} 含本地草稿无法表达的执行语义，禁止普通同步降级覆盖')
                    step['action']='update';step['lockVersion']=existing.get('lockVersion')
                    if not isinstance(existing.get('lockVersion'),int):blockers.append(f'{"已有同名同类型共享属性 " if kind=="attribute" else ""}{data["code"]} 缺少 lockVersion，禁止覆盖')
            for remote_kind,remote_id in occupied.get(data['code'].casefold(),[]):
                if remote_kind!=kind or remote_id!=data['id']:blockers.append(f'目标分支 CODE 冲突：{data["code"]}')
    seen={}
    for step in steps:
        c=step['data']['code'].casefold()
        if c in seen and seen[c]!=(step['kind'],step['local_id']):blockers.append(f'本地 CODE 冲突 {c}')
        seen[c]=(step['kind'],step['local_id'])
    return dict(ready=not blockers,blockers=list(dict.fromkeys(blockers)),warnings=list(dict.fromkeys(warnings)),
                project_revision=snap['project']['revision'],domainId=domain,branchId=branch,steps=steps,
                counts={k:sum(x['kind']==k for x in steps) for k in KINDS},
                note='只写类型定义；HTTP 单对象事务，非批量原子事务；不包含资源绑定和正式上线')

class OpenMindClient:
    def __init__(self,base_url=None,token=None,transport=None,allowed_hosts=None):
        self.allowed_hosts=allowed_hosts
        self.url=validate_base_url(base_url if base_url is not None else os.getenv('OPENMIND_BASE_URL',''), allowed_hosts)
        self.token=token if token is not None else os.getenv('OPENMIND_ACCESS_TOKEN','')
        if not self.token:raise OpenMindError('OpenMind 用户访问 Token 未配置，请在“OpenMind 对接”页面配置')
        # HTTP-intranet mode must not silently forward credentials via HTTP_PROXY.
        self.client=httpx.Client(base_url=self.url,transport=transport,follow_redirects=False,
                                 trust_env=not self.url.startswith('http://'),timeout=25)
    def close(self):self.client.close()
    def request(self,method,path,params=None,data=None):
        if self.url.startswith('http://'):
            validate_base_url(self.url,self.allowed_hosts)  # recheck DNS / deployment policy
        try:
            r=self.client.request(method,path,params=params,json=data,
                                  headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            host=urlsplit(self.url).hostname or '未知主机'
            port=urlsplit(self.url).port or (443 if self.url.startswith('https:') else 80)
            hint=("容器内 localhost/127.0.0.1 指向 OntoFlow 自己；若 OpenMind 在 Mac 宿主机，请使用 host.docker.internal 与实际服务端口。"
                  if host in ('localhost','127.0.0.1','::1') else
                  "请在 OntoFlow 容器内检查主机名 DNS 解析、端口监听、Docker 网络、防火墙与代理/TLS 配置。")
            reason='连接超时' if isinstance(exc,httpx.ConnectTimeout) else '无法建立连接'
            raise OpenMindError(f'OpenMind {reason}（{host}:{port}，{type(exc).__name__}）；'
                                f'尚未取得 HTTP 响应。{hint}') from exc
        except httpx.TimeoutException as exc:
            suffix=('写入请求可能已到达服务器；请先回读目标分支 catalog 核对稳定 UUID，再决定是否重试。'
                    if method.upper() not in ('GET','HEAD','OPTIONS') else
                    '这是只读请求，检查服务器响应速度、代理及网络后可重试。')
            raise OpenMindError(f'OpenMind 请求超时（{type(exc).__name__}）。{suffix}') from exc
        except (httpx.WriteError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            suffix=('写入结果未确定；必须先回读 catalog 核对稳定 UUID，避免重复写入。'
                    if method.upper() not in ('GET','HEAD','OPTIONS') else
                    '只读操作失败，可排查网络后重试。')
            raise OpenMindError(f'OpenMind 通信中断（{type(exc).__name__}）。{suffix}') from exc
        except httpx.HTTPError as exc:
            raise OpenMindError(f'OpenMind 网络错误（{type(exc).__name__}）。请检查连接设置及网络环境。') from exc
        if r.status_code>=300:
            if r.status_code in (301,302,307,308):
                raise OpenMindError(f'OpenMind HTTP {r.status_code} 重定向；请填写实际 API 服务根地址（协议、端口均需正确），客户端不会自动携带令牌跟随跳转。')
            if r.status_code in (401,403):
                raise OpenMindError(f'OpenMind HTTP {r.status_code}：目标服务已响应，但访问令牌无效或缺少权限；请核对页面保存的用户 Bearer Token。')
            if r.status_code==404:
                raise OpenMindError('OpenMind HTTP 404：网络已连接，但 API 路径不存在；请检查是否配置了正确的后端服务根地址（不要填写前端地址或 /api/v1 子路径）。')
            try:body=r.json()
            except ValueError:body={'status':r.status_code}
            raise OpenMindError(f'OpenMind HTTP {r.status_code}: {json.dumps(body,ensure_ascii=False)[:700]}')
        if r.status_code==204:return None
        try:return r.json()
        except ValueError:raise OpenMindError('OpenMind 未返回 JSON')
    def domains(self):return self.request('GET','/api/v1/domains')
    def catalog(self,branch):return self.request('GET','/api/v1/catalog',params={'branchId':branch})
    def graph(self,branch):return self.request('GET','/api/v1/graph',params={'branchId':branch})
    def types(self):return self.request('GET','/api/v1/ontology/attribute-types')
    def write(self,branch,step,existing=None):
        kind,data=step['kind'],step['data'];path=f'/api/v1/branches/{branch}/{kind}'
        if existing is not None:
            if not isinstance(existing.get('lockVersion'),int):raise OpenMindError('更新缺少远端 lockVersion')
            merged=prepare_write(step,existing)
            return self.request('PUT',path+'/'+data['id'],data={'data':merged,'lockVersion':existing['lockVersion']})
        return self.request('POST',path,data={'data':data})
