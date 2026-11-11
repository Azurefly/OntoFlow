"""Incremental human-in-the-loop ontology workbench.
Run: uvicorn app.main:app --reload (from project root)
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import logging
import threading
import traceback
from pathlib import Path
from .version import VERSION, build_identity
from .sync_engine import SyncSession, SyncBusy
from .task_leases import Lease, LostLease, lease_scope, claim_job
from .gap_state import record_gap
from datetime import datetime,timedelta,timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .store import Store,uid,now,j
from .extraction import SUPPORTED, extract, ExtractionError
from .format_registry import capabilities, require_reader
from .metrics import attach_to_job_result
from .ontology import validate_payload,check_ontology,serialize_ontology,InvalidOntology
from .naming import prepare_payload,naming_errors,save_metadata,code_error,relation_name,is_chinese_name,assignment_error
from . import brain, incremental
from .openmind import OpenMindClient,OpenMindError,make_plan,parse_catalog,uuid_value,code_for,CODE
from .openmind_auth import OpenMindAutoClient
from .model_settings import ModelSettings, ModelSettingsError, validate_endpoint
from . import modeling
from .modeling import ModelingIntentIn
from .source_understanding import material_profile
from .demo_mode import enabled as demo_enabled, connection_policy
from .openmind_settings import OpenMindSettings,validate_connection
from .openmind_baseline import (canonical_catalog, baseline_hash, baseline_public,
    baseline_objects, duplicate_against_baseline)

STAGES=('UPLOAD','TOPIC','ENTITY','PROPERTY','RELATION','REVIEW')
KIND_STAGE={'entity':'ENTITY','attribute':'PROPERTY','relation':'RELATION'}
MAX_UPLOAD=int(os.getenv('MAX_UPLOAD_MB','20'))*1024*1024


class ProjectDeleteIn(BaseModel):
    confirm_name:str=Field(min_length=1,max_length=120)
    expected_revision:int=Field(ge=0)


class GapDecisionIn(BaseModel):
    action:str
    note:str=Field(min_length=1,max_length=2000)
    expected_version:int=Field(ge=0)


class ProjectIn(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    domain_id:str|None=None
    branch_id:str|None=None


class RecheckIntentIn(BaseModel):
    expected_revision:int=Field(ge=0)
    note:str=Field(min_length=2,max_length=2000)


class TopicIn(BaseModel):
    summary:str=Field(min_length=1,max_length=10000)
    scope:str=Field(min_length=1,max_length=5000)


class ProposalIn(BaseModel):
    kind:str
    payload:dict
    evidence:list=Field(default_factory=list)


class DecisionIn(BaseModel):
    action:str
    payload:dict|None=None
    note:str=Field(default='',max_length=2000)
    expected_revision:int|None=None


class BulkDecisionIn(BaseModel):
    kind:str
    proposal_ids:list[str]=Field(min_length=1,max_length=200)
    action:str
    note:str=Field(default='',max_length=2000)
    expected_revision:int


class ReviewAllIn(BaseModel):
    kind:str
    expected_revision:int
    fingerprint:str=Field(min_length=64,max_length=64)
    confirmation:str
    note:str=Field(default='跨页全部同意',max_length=2000)


class SyncConflictIn(BaseModel):
    expected_revision:int
    observation_hash:str=Field(min_length=64,max_length=64)
    confirmation:str
    note:str=Field(min_length=1,max_length=2000)


class ChatIn(BaseModel):
    message:str=Field(min_length=1,max_length=4000)


class StageIn(BaseModel):
    target:str


class PublishIn(BaseModel):
    note:str=Field(default='人工审核发布',max_length=500)


class OpenMindSettingsIn(BaseModel):
    base_url:str=Field(min_length=1,max_length=1024)
    access_token:str=Field(default='',max_length=8000)
    allowed_hosts:list[str]=Field(default_factory=list,max_length=50)
    clear_access_token:bool=False
    new_admin_token:str=Field(default='',max_length=2048)
    clear_admin_token:bool=False
    auth_mode:str|None=None
    username:str=Field(default='',max_length=128)
    password:str=Field(default='',max_length=512)
    clear_password:bool=False


class OpenMindBindingIn(BaseModel):
    domain_id:str
    branch_id:str


class OpenMindCodeIn(BaseModel):
    kind:str
    local_id:str
    code:str


class OpenMindSyncIn(BaseModel):
    expected_revision:int
    confirmation:str


class ModelSettingsIn(BaseModel):
    base_url:str=Field(default='',max_length=1024)
    model:str=Field(default='',max_length=180)
    api_key:str=Field(default='',max_length=8000)
    clear_api_key:bool=False


def create_app(data_root: str | Path | None=None, *, testing: bool=False):
    # Real production deployments must never silently create a new SQLite DB.
    # The explicit testing flag is used only by legacy SQLite unit tests.
    if os.getenv("APP_ENV", "development") == "production" and not os.getenv("DATABASE_URL", "").strip() and not testing:
        raise RuntimeError("生产部署必须配置 PostgreSQL DATABASE_URL；已拒绝静默回退 SQLite。")
    if os.getenv('APP_ENV','development')=='production' and not demo_enabled() and not os.getenv('API_TOKEN'):
        raise RuntimeError('生产模式必须配置 API_TOKEN，并在可信网关验证用户身份。')
    base=Path(data_root or os.getenv('DATA_DIR','./data'))
    store=Store(base)
    model_settings=ModelSettings(store)
    openmind_settings=OpenMindSettings(store)
    app=FastAPI(title='渐进式本体构建服务',version=VERSION,description='文件→摘要→主题→实体→属性→关系→协商→审核→发布')
    app.state.store=store
    app.state.model_settings=model_settings
    app.state.openmind_settings=openmind_settings
    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")
    origins=[x.strip() for x in os.getenv('CORS_ORIGINS','').split(',') if x.strip()]
    if origins:
        app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type','X-API-Key','X-Actor','X-Model-Admin-Key','X-OpenMind-Admin-Token'])

    def authorize(request:Request):
        if demo_enabled():
            return 'local-demo'
        token=os.getenv('API_TOKEN','')
        if token and not secrets.compare_digest(request.headers.get('x-api-key',''),token):
            raise HTTPException(401,'无效的 API Key。')
        # X-Actor must only be supplied by a trusted reverse proxy in real deployments.
        return re.sub(r'[^a-zA-Z0-9_@.:-]','_',request.headers.get('x-actor','local-demo'))[:100]

    def authorize_model_admin(request:Request):
        actor=authorize(request)
        if demo_enabled():
            return actor
        admin_token=os.getenv('MODEL_ADMIN_TOKEN', '')
        if admin_token and not secrets.compare_digest(request.headers.get('x-model-admin-key', ''),admin_token):
            raise HTTPException(403,'需要模型配置管理员权限（X-Model-Admin-Key）。')
        return actor

    def openmind_admin(request:Request):
        actor=authorize(request)
        if demo_enabled():
            return actor
        supplied=request.headers.get('x-openmind-admin-token','')
        # The existing model administrator can bootstrap or recover the OpenMind settings.
        model_admin=os.getenv('MODEL_ADMIN_TOKEN','')
        if model_admin and secrets.compare_digest(request.headers.get('x-model-admin-key',''),model_admin):
            return actor
        if openmind_settings.has_admin():
            if not openmind_settings.verify_admin(supplied):
                raise HTTPException(403,'OpenMind 对接管理员验证失败（或使用模型管理员身份）。')
            return actor
        if os.getenv('APP_ENV')=='production':
            if model_admin:
                raise HTTPException(403,'需要有效模型管理员身份进行 OpenMind 首次配置。')
            raise HTTPException(503,'生产首次配置需要 MODEL_ADMIN_TOKEN 初始化管理员；随后可在页面设置 OpenMind 管理密钥。')
        return actor

    # A single worker owns one rotating-cookie session. Each caller gets a
    # no-close lease so previous call sites can safely use try/finally close().
    client_guard=threading.RLock()
    shared={'key':None,'client':None}
    class ClientLease:
        def __init__(self, client): self._client=client
        def __getattr__(self,name):return getattr(self._client,name)
        def close(self):pass

    def make_openmind_client():
        conf=openmind_settings.effective()
        validate_connection(conf['base_url'],conf['allowed_hosts'])
        if conf.get('auth_mode')!='login':
            return OpenMindClient(conf['base_url'],conf['access_token'],allowed_hosts=conf['allowed_hosts'])
        # Do not expose or persist access/refresh tokens. The config digest
        # invalidates the shared session when account, secret or endpoint changes.
        identifier=hashlib.sha256(json.dumps({k:conf.get(k) for k in
            ('base_url','allowed_hosts','auth_mode','username','password')},sort_keys=True).encode()).hexdigest()
        with client_guard:
            if shared['key']!=identifier or shared['client'] is None:
                if shared['client'] is not None: shared['client'].close()
                shared['client']=OpenMindAutoClient(conf['base_url'],conf['username'],conf['password'],allowed_hosts=conf['allowed_hosts'])
                shared['key']=identifier
            return ClientLease(shared['client'])

    @app.on_event('shutdown')
    def shutdown_openmind_session():
        with client_guard:
            if shared['client'] is not None:shared['client'].close()
            shared.update(key=None,client=None)

    def visible_domains(client):
        raw=client.domains()
        if not isinstance(raw,dict) or not isinstance(raw.get('items'),list):
            raise OpenMindError('OpenMind /api/v1/domains 未返回 {items:[]}，无法安全选择分支')
        output=[]
        for item in raw['items']:
            if not isinstance(item,dict) or not isinstance(item.get('branches'),list):
                raise OpenMindError('OpenMind 领域目录缺少 branches 数组')
            domain_id=uuid_value(item.get('id'),'OpenMind 领域 ID')
            branch_rows=[]
            for branch in item['branches']:
                if not isinstance(branch,dict):raise OpenMindError('OpenMind 分支信息无效')
                branch_id=uuid_value(branch.get('id'),'OpenMind 分支 ID')
                writable=(branch.get('branchType')=='development' and branch.get('status')=='draft' and item.get('type')=='local')
                branch_rows.append({'id':branch_id,'version':str(branch.get('version','')),
                                    'status':str(branch.get('status','')),'branchType':str(branch.get('branchType','')),
                                    'selectable':writable})
            output.append({'id':domain_id,'name':str(item.get('name','')),'code':str(item.get('code','')),
                           'type':item.get('type','local'),'branches':branch_rows})
        return output

    def openmind_binding(pid):
        get_project(pid)
        return store.one('SELECT * FROM openmind_bindings WHERE project_id=?',(pid,))

    def openmind_endpoint_hash():
        # Bind cached branch data to endpoint AND authenticated identity.
        # Login mode retains baseline across token rotation. Manual token mode
        # cannot know the account without /me, so credential changes invalidate.
        conf=openmind_settings.effective()
        # Cache identity must change when the service account secret rotates:
        # a username can be reissued to another account after deletion.
        identity=(conf.get('username','').casefold() + ':' +
                  hashlib.sha256(conf.get('password','').encode()).hexdigest()
                  if conf.get('auth_mode')=='login' else
                  hashlib.sha256(conf.get('access_token','').encode()).hexdigest())
        key=json.dumps([conf.get('base_url','').strip().rstrip('/').lower(),conf.get('auth_mode'),identity])
        return hashlib.sha256(key.encode()).hexdigest()

    def baseline_row(pid):
        return store.one('SELECT * FROM openmind_baselines WHERE project_id=?',(pid,))

    def current_baseline(pid):
        binding=openmind_binding(pid)
        if not binding:return None
        row=baseline_row(pid)
        if not row or row['branch_id']!=binding['branch_id'] or row['domain_id']!=binding['domain_id'] or row['endpoint_hash']!=openmind_endpoint_hash():
            return None
        return baseline_objects(row)

    def require_baseline(pid):
        binding=openmind_binding(pid)
        if not binding:return None
        baseline=current_baseline(pid)
        if baseline is None:
            raise HTTPException(409,'已绑定 OpenMind 分支，但尚未成功读取该分支的现有实体、属性、关系。请先在“OpenMind 对接”中刷新分支基线；禁止降级为只分析文档。')
        return baseline

    def save_baseline(pid, raw, actor='system', own_write=False):
        """Atomically store the full native catalog and local *read-only* entity stubs.

        Remote attributes and relationships keep their complete native shape in
        the catalog instead of being flattened into one-owner/one-edge tables.
        """
        binding=openmind_binding(pid)
        if not binding:raise OpenMindError('未绑定目标分支')
        catalog=canonical_catalog(raw)
        fingerprint=baseline_hash(catalog)
        endpoint_hash=openmind_endpoint_hash()
        existing_row=baseline_row(pid)
        mapped_remote_ids={x['remote_id'] for x in store.many(
            "SELECT remote_id FROM openmind_mappings WHERE project_id=? AND kind='entity'",(pid,))}
        remote_entities={e['id']:e for e in catalog['entities'] if e['id'] not in mapped_remote_ids}
        with store.tx() as c:
            locals_=[dict(x) for x in c.execute('SELECT id,name,kind,origin FROM entities WHERE project_id=?',(pid,)).fetchall()]
            local_names={(x['name'].casefold(),x['kind']):x for x in locals_ if x['origin']!='openmind'}
            shadow_ids={x['id'] for x in locals_ if x['origin']=='openmind'}
            problems=[]
            for eid,e in remote_entities.items():
                obj=c.execute('SELECT origin FROM entities WHERE project_id=? AND id=?',(pid,eid)).fetchone()
                if obj and obj['origin']!='openmind':
                    problems.append(f'{e["code"]}: UUID 与本地已确认实体重合')
                found=local_names.get((e['name'].casefold(),'class'))
                if found and found['id']!=eid:
                    problems.append(f'已有本地实体「{e["name"]}」与 OpenMind {e["code"]} 重名；请人工判断复用或合并，不自动覆盖')
            # Preserve the same semantics as the local uniqueness constraint.
            names={}
            for eid,e in remote_entities.items():
                name=e['name'].casefold()
                if name in names and names[name]!=eid:
                    problems.append(f'OpenMind 同名实体「{e["name"]}」无法映射为本地单名称编辑结构，请先解决歧义')
                names[name]=eid
            removed=shadow_ids-set(remote_entities)
            for eid in removed:
                a=c.execute('SELECT id FROM attributes WHERE project_id=? AND owner_id=? LIMIT 1',(pid,eid)).fetchone()
                r=c.execute('SELECT id FROM relations WHERE project_id=? AND (source_id=? OR target_id=?) LIMIT 1',(pid,eid,eid)).fetchone()
                if a or r:problems.append(f'远端实体 {eid} 已删除，但本地增量仍引用它；请先人工处理引用冲突')
            if problems:
                raise OpenMindError('OpenMind 目录与本地已确认增量发生冲突：'+'；'.join(problems[:8]))
            for eid in removed:
                c.execute("DELETE FROM entities WHERE project_id=? AND id=? AND origin='openmind'",(pid,eid))
            for eid,e in remote_entities.items():
                c.execute("""INSERT INTO entities (id,project_id,name,kind,description,class_id,evidence,origin)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id,id) DO UPDATE SET
                    name=excluded.name,description=excluded.description
                    WHERE entities.origin='openmind' AND entities.project_id=excluded.project_id""",
                    (eid,pid,e['name'],'class',str(e.get('description',''))[:1000],None,'[]','openmind'))
            counts={k:len(catalog[k]) for k in ('entities','attributes','relations','actions','capabilities')}
            c.execute("""INSERT INTO openmind_baselines
                (project_id,domain_id,branch_id,endpoint_hash,catalog_json,fingerprint,entity_count,attribute_count,
                 relation_count,action_count,capability_count,refreshed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                domain_id=excluded.domain_id,branch_id=excluded.branch_id,endpoint_hash=excluded.endpoint_hash,
                catalog_json=excluded.catalog_json,fingerprint=excluded.fingerprint,
                entity_count=excluded.entity_count,attribute_count=excluded.attribute_count,
                relation_count=excluded.relation_count,action_count=excluded.action_count,
                capability_count=excluded.capability_count,refreshed_at=excluded.refreshed_at""",
                (pid,binding['domain_id'],binding['branch_id'],endpoint_hash,j(catalog),fingerprint,
                 counts['entities'],counts['attributes'],counts['relations'],counts['actions'],counts['capabilities'],now()))
            changed=not existing_row or existing_row['fingerprint']!=fingerprint or existing_row['endpoint_hash']!=endpoint_hash
            # Remote changes invalidate a previously published local snapshot,
            # unless this is the read-back of our own approved write operations.
            if changed and not own_write:
                c.execute("UPDATE proposals SET status='rejected',decided_at=?,review_note='OpenMind 分支基线变化，原候选需重新比对' WHERE project_id=? AND status='pending'",(now(),pid))
                c.execute('DELETE FROM proposal_progress WHERE project_id=?',(pid,))
                store.revision(c,pid)
                store.record_action(c,pid,'TOPIC',
                    f'用户已从 OpenMind 分支 {binding["branch_id"]} 加载现有本体：'
                    f'{counts["entities"]} 个实体、{counts["attributes"]} 个属性、{counts["relations"]} 个关系。',
                    '现有分支定义已作为构建基线；后续文档仅用于发现需要扩展或澄清的部分。'
                    '远端对象保留原生结构，未经审核不修改已有定义。')
            store.audit(c,pid,actor,'openmind_baseline_refreshed',{'fingerprint':fingerprint,
                        'counts':counts,'changed':changed,'own_write':own_write})
        return {**baseline_public(baseline_row(pid)),'changed':changed,'project_revision_changed':changed and not own_write}

    def refresh_baseline(pid,actor):
        client=make_openmind_client();session=None
        try:
            binding=openmind_binding(pid)
            if not binding:raise OpenMindError('请先绑定目标分支')
            session=SyncSession(store,pid,openmind_endpoint_hash(),binding['branch_id'],get_project(pid)['revision'],actor)
            session.acquire()
            catalog=session.catalog(client)
            cached=baseline_row(pid)
            own=False
            if cached and cached['endpoint_hash']==openmind_endpoint_hash() and cached['branch_id']==binding['branch_id']:
                own=session.reconcile(json.loads(cached['catalog_json']),catalog,allow_external=True)
            result=save_baseline(pid,catalog,actor,own_write=own)
            return result
        finally:
            client.close()
            if session:session.release('readback_finished',{'reconciled':session.reconciled})

    def openmind_maps(pid):
        rows=store.many('SELECT * FROM openmind_mappings WHERE project_id=?',(pid,))
        codes=store.many('SELECT * FROM openmind_codes WHERE project_id=?',(pid,))
        return ({(r['kind'],r['local_id']):r for r in rows},
                {(r['kind'],r['local_id']):r['code'] for r in codes})

    def openmind_snapshot(pid):
        p=get_project(pid)
        latest=store.one('SELECT * FROM releases WHERE project_id=? ORDER BY version DESC LIMIT 1',(pid,))
        if not latest:raise HTTPException(409,'请先在 OntoFlow 人工审核并发布一个本地版本。')
        if latest['revision']!=p['revision']:
            raise HTTPException(409,'当前草稿有未发布改动；必须先审完并发布新版本。')
        return json.loads(latest['snapshot']),latest

    def openmind_preview(pid,catalog=None):
        binding=openmind_binding(pid)
        if not binding:raise HTTPException(409,'请先绑定 OpenMind 根领域和可写 development 分支。')
        snap,latest=openmind_snapshot(pid)
        mappings,codes=openmind_maps(pid)
        plan=make_plan(snap,pid,binding['domain_id'],binding['branch_id'],mappings,codes,catalog)
        plan['ontoflow_version']=latest['version']
        logs=store.many('SELECT id,kind,remote_id,code,state FROM openmind_operations WHERE project_id=? AND endpoint_hash=? AND branch_id=? ORDER BY created_at,id',
                        (pid,openmind_endpoint_hash(),binding['branch_id']))
        latest_ops={(op['kind'],op['remote_id']):op for op in logs}
        conflicts=[op for op in latest_ops.values() if op['state']=='CONFLICT']
        if conflicts:
            plan['ready']=False
            plan['blockers'].append(f'仍有 {len(conflicts)} 项未解决远端写入冲突，请先打开“写入冲突与恢复”重新核对；不会自动覆盖')
            plan['conflicts']=[{'id':op['id'],'kind':op['kind'],'code':op['code']} for op in conflicts]
        return plan

    def active_model_config():
        try:
            return model_settings.effective()
        except ModelSettingsError as e:
            raise HTTPException(503, str(e)) from e

    def get_project(pid):
        p=store.project(pid)
        if not p:raise HTTPException(404,'项目不存在。')
        return p

    def docs(pid):
        return store.many('SELECT * FROM documents WHERE project_id=? ORDER BY created_at,id',(pid,))

    def public_doc(d):
        item={k:d[k] for k in ('id','project_id','filename','sha256','bytes','warnings','created_at') if k in d}
        item['warnings']=json.loads(item.get('warnings','[]'))
        return item

    def project_view(pid):
        p=get_project(pid)
        return {'project':p,'documents':[public_doc(d) for d in docs(pid)],
                'snapshot':store.snapshot(pid),
                'proposals':public_proposals(store.many('SELECT * FROM proposals WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 60',(pid,)),pid),
                'modeling_context':modeling.context(store,pid),
                'pending_totals':{r['kind']:r['n'] for r in store.many(
                    "SELECT kind,COUNT(*) AS n FROM proposals WHERE project_id=? AND status='pending' GROUP BY kind",(pid,))},
                'proposal_progress':{r['stage']:{'processed_batches':r['next_batch'],'total_batches':r['total_batches']}
                     for r in store.many('SELECT stage,next_batch,total_batches FROM proposal_progress WHERE project_id=?',(pid,))},
                'openmind_baseline':baseline_public(baseline_row(pid)) if current_baseline(pid) is not None else None,
                'openmind_bound':bool(openmind_binding(pid)),
                'jobs_latest':{'review_all':store.one("SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind='review_all' ORDER BY rowid DESC LIMIT 1",(pid,)),
                'parse':store.one("SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind='parse' ORDER BY rowid DESC LIMIT 1",(pid,)),
                'summary':store.one("SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind='summary' ORDER BY rowid DESC LIMIT 1",(pid,)),
                   **{s:store.one("SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind='propose' AND stage=? ORDER BY rowid DESC LIMIT 1",(pid,s))
                   for s in ('ENTITY','PROPERTY','RELATION')}},
                'messages':list(reversed(store.many('SELECT * FROM messages WHERE project_id=? ORDER BY rowid DESC LIMIT 200',(pid,)))),
                'assistant_progress':list(reversed(store.many('SELECT * FROM job_progress_events WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 150',(pid,)))),
                'extraction_gaps':store.many("SELECT * FROM extraction_gaps WHERE project_id=? AND status IN ('open','retrying') ORDER BY rowid DESC LIMIT 80",(pid,)),
                'active_jobs':store.many("SELECT * FROM workflow_jobs WHERE project_id=? AND state IN ('queued','running') ORDER BY created_at,id LIMIT 20",(pid,)),
                'releases':store.many('SELECT id,version,revision,note,created_at FROM releases WHERE project_id=? ORDER BY version DESC',(pid,)),
                'engine':{'mode':model_settings.public()['mode'],'model':model_settings.public()['model']},
                'validation':check_ontology(store.snapshot(pid))}

    def _public_proposal(row):
        payload=json.loads(row['payload']);evidence=json.loads(row['evidence'])
        return {**row,'payload':prepare_payload(row['kind'],payload,evidence),
                'evidence':evidence}

    def configured_model(pid, config=None):
        return modeling.model_config(store,pid,config if config is not None else active_model_config())

    def public_proposals(rows,pid):
        result=[]
        with store.tx(read_only=True) as c:
            runtime=modeling.runtime_in_tx(c,pid)
            origins={r['proposal_id']:r for r in c.execute('SELECT * FROM proposal_origins WHERE project_id=?',(pid,)).fetchall()}
            for row in rows:
                out=_public_proposal(row)
                origin=origins.get(row['id'])
                if origin:
                    basis=json.loads(origin['basis'])
                    out['basis']={**basis,'label':modeling.LABELS.get(basis.get('kind'),'建模推断'),
                        'context_stale':bool(origin['context_fingerprint'] and origin['context_fingerprint']!=runtime['fingerprint'])}
                else:out['basis']={'kind':'legacy','label':'历史候选（未分类）','context_stale':runtime.get('decision_version',0)>0}
                result.append(out)
        return result

    @app.get('/api/projects/{pid}/modeling-context',dependencies=[Depends(authorize)])
    def get_modeling_context(pid:str):
        get_project(pid)
        return {'context':modeling.context(store,pid),'runtime':modeling.runtime(store,pid),
                'history':[{**r,'before_spec':json.loads(r['before_spec']),'after_spec':json.loads(r['after_spec'])}
                    for r in store.many('SELECT * FROM modeling_decisions WHERE project_id=? ORDER BY version DESC LIMIT 30',(pid,))]}

    @app.put('/api/projects/{pid}/modeling-context',dependencies=[Depends(authorize)])
    def update_modeling_context(pid:str,body:ModelingIntentIn,request:Request):
        get_project(pid)
        if set(body.include_types)&set(body.exclude_types):
            raise HTTPException(422,'同一类型不能同时纳入与排除')
        actor=authorize(request)
        with store.tx() as c:
            p=c.execute('SELECT stage,revision FROM projects WHERE id=?'+(' FOR UPDATE' if store.is_postgres else ''),(pid,)).fetchone()
            old=modeling.context_in_tx(c,pid)
            if old['version']!=body.expected_version:raise HTTPException(409,'建模意图已被修改，请刷新后确认')
            spec={key:getattr(body,key) for key in modeling.FIELDS}
            version=old['version']+1
            c.execute('''INSERT INTO modeling_contexts(project_id,version,spec,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,spec=excluded.spec,updated_at=excluded.updated_at''',
               (pid,version,j(spec),now()))
            c.execute('INSERT INTO modeling_decisions VALUES(?,?,?,?,?,?,?,?)',
               (uid(),pid,version,j({key:old[key] for key in modeling.FIELDS}),j(spec),body.note.strip(),actor,now()))
            store.revision(c,pid)
            c.execute('UPDATE projects SET proposal_generation=proposal_generation+1 WHERE id=?',(pid,))
            store.record_action(c,pid,p['stage'],
                f'用户确认建模意图 v{version}：{body.goal.strip() or "沿用已确认主题"}；角色策略 {body.role_strategy}；'+
                (f'补充决定：{body.instructions.strip()}' if body.instructions.strip() else '未补充原文以外的业务定义。'),
                '这是用户建模决定，不是原文事实。旧候选需按当前意图复核；中途变更的任务会停止旧上下文提交。已审核本体不自动修改。')
            store.audit(c,pid,actor,'modeling_intent_confirmed',{'version':version,'note':body.note,'spec':spec})
        return {'context':modeling.context(store,pid),'revision':get_project(pid)['revision']}

    @app.get('/api/projects/{pid}/understanding',dependencies=[Depends(authorize)])
    def document_understanding(pid:str):
        get_project(pid)
        return {'materials':material_profile(docs(pid)), 'context':modeling.context(store,pid),
                'notice':'文档格式只决定读取器；类别和标题不自动成为本体类型。用户补充要求单独记录，不伪装成原文。'}

    @app.post('/api/projects/{pid}/proposals/{qid}/recheck-intent',dependencies=[Depends(authorize)])
    def recheck_proposal_intent(pid:str,qid:str,body:RecheckIntentIn,request:Request):
        get_project(pid)
        with store.tx() as c:
            project=c.execute('SELECT revision,stage FROM projects WHERE id=?'+(' FOR UPDATE' if store.is_postgres else ''),(pid,)).fetchone()
            if project['revision']!=body.expected_revision:raise HTTPException(409,'本体修订已变化，请刷新后复核')
            q=c.execute("SELECT * FROM proposals WHERE id=? AND project_id=? AND status='pending'",(qid,pid)).fetchone()
            if not q:raise HTTPException(404,'待审候选不存在')
            old=c.execute('SELECT basis FROM proposal_origins WHERE proposal_id=?',(qid,)).fetchone()
            runtime=modeling.runtime_in_tx(c,pid)
            basis={'kind':'user_decision','method':'explicit_recheck','rationale':body.note.strip(),
                   'previous_basis':{key:json.loads(old['basis']).get(key) for key in ('kind','method','rationale')} if old else {'kind':'legacy'}}
            modeling.save_proposal_origin(c,pid,qid,basis,runtime['fingerprint'])
            c.execute('UPDATE projects SET proposal_generation=proposal_generation+1 WHERE id=?',(pid,))
            store.record_action(c,pid,project['stage'],f'用户按当前建模意图复核候选：{body.note.strip()}',
                '复核依据记录为用户决定；候选仍待审批，不自动写入本体或 OpenMind。')
            store.audit(c,pid,authorize(request),'proposal_intent_rechecked',{'proposal_id':qid,'note':body.note})
        return {'rechecked':True,'proposal_id':qid,'status':'pending'}

    @app.get('/api/projects/{pid}/proposals/page',dependencies=[Depends(authorize)])
    def paged_proposals(pid:str,kind:str='entity',status:str='pending',page:int=1,page_size:int=20):
        get_project(pid)
        if kind not in KIND_STAGE or status not in ('pending','accepted','rejected','all'):
            raise HTTPException(422,'候选类型或状态无效')
        if not 1<=page<=100000 or not 1<=page_size<=100:
            raise HTTPException(422,'page/page_size 无效')
        cond="project_id=? AND kind=?" + ("" if status=='all' else " AND status=?")
        args=(pid,kind) if status=='all' else (pid,kind,status)
        total=store.one(f'SELECT COUNT(*) AS n FROM proposals WHERE {cond}',args)['n']
        rows=store.many(f"SELECT * FROM proposals WHERE {cond} ORDER BY created_at ASC,id ASC LIMIT ? OFFSET ?",args+(page_size,(page-1)*page_size))
        return {'kind':kind,'status':status,'page':page,'page_size':page_size,
                'total':total,'items':public_proposals(rows,pid)}

    def validated_evidence(pid,ev,context=None):
        known=context.get('known_docs') if context is not None else None
        if known is None:
            known={d['id']:d for d in docs(pid)}
            if context is not None:context['known_docs']=known
        result=[]
        if not isinstance(ev,list): return result
        from .schema_catalog import attribute_identity_from_chunk
        parsed=context.setdefault('parsed_docs',{}) if context is not None else {}
        for x in ev[:10]:
            if not isinstance(x,dict):continue
            doc_id=str(x.get('document_id',''))
            if doc_id not in known:continue
            text=str(x.get('quote',''))[:600]
            locator=str(x.get('locator',''))[:140]
            content=known[doc_id]['content']
            verified=bool(text and text in content)
            item={'document_id':doc_id,'locator':locator,'quote':text,'verified':verified}
            # Trusted source CODE/UUID for native JSON requires BOTH an exact
            # JSONPath locator and a matching signed-off object in the server's
            # stored upload. Client-supplied source_code is NEVER accepted.
            if verified and locator.startswith('$.'):
                if doc_id not in parsed:
                    chunks=known[doc_id].get('chunks') or '[]'
                    parsed[doc_id]=json.loads(chunks) if isinstance(chunks,str) else chunks
                for chunk in parsed[doc_id]:
                    if chunk.get('locator')!=locator or chunk.get('kind') not in (
                            'native-entity','native-attribute','native-relation','native-unbound-attribute'):
                        continue
                    try:source=json.loads(chunk['text'])
                    except (ValueError,TypeError):break
                    kind=chunk['kind']
                    expected=(source.get('code') if kind=='native-relation' else source.get('name'))
                    if expected is None or json.dumps(str(expected),ensure_ascii=False)[1:-1]!=text:break
                    code=str(source.get('code') or '').strip()
                    if re.fullmatch(r'[a-z][a-z0-9_]*',code):
                        item.update(source_code=code,source_name=str(source.get('name') or ''),
                                    source_uuid=str(source.get('id') or ''),
                                    source_branch_id=str(source.get('branchId') or ''))
                    break
            if verified and text.startswith('|'):
                # Never trust a submitted source_code; derive it from the
                # actual uploaded table HEADER and the same quoted row.
                if doc_id not in parsed:
                    chunks=known[doc_id].get('chunks') or '[]'
                    parsed[doc_id]=json.loads(chunks) if isinstance(chunks,str) else chunks
                for chunk in parsed[doc_id]:
                    if chunk.get('kind')!='table' or text not in chunk.get('text',''):
                        continue
                    identity=attribute_identity_from_chunk(chunk,text)
                    if identity:
                        item.update(source_code=identity[0],source_name=identity[1])
                        break
            result.append(item)
        return result

    def add_proposals(pid,stage,items,origin,actor,batch_number=0,context=None):
        if stage not in KIND_STAGE.values(): raise HTTPException(400,'当前阶段不能生成本体候选。')
        require_baseline(pid)
        created=[];rejected=[];reused=[];omitted=[]
        with store.tx() as c:
            current_project=c.execute('SELECT stage,scope_confirmed,revision,proposal_generation FROM projects WHERE id=?'+(' FOR UPDATE' if store.is_postgres else ''),(pid,)).fetchone()
            if not current_project or not current_project['scope_confirmed'] or current_project['stage']!=stage:
                raise HTTPException(409,'阶段或建模主题发生变化，请刷新后重新生成候选。')
            policy=modeling.runtime_in_tx(c,pid)
            if context and context.get('policy_fingerprint') and context['policy_fingerprint']!=policy['fingerprint']:
                raise HTTPException(409,'建模意图或目标分支已改变，拒绝提交旧上下文的模型建议；请重新生成')
            def origin_for(item):
                if origin=='human':return {'kind':'user_decision','method':'manual_candidate','rationale':'用户手工提出；不声称原文已经包含该定义'}
                declared=(item.get('_basis') or {}) if context and context.get('trusted_basis') else {}
                kind=declared.get('kind') if declared.get('kind') in ('source_explicit','inference') else 'inference'
                return {'kind':kind,'method':declared.get('method','llm_or_rule_suggestion'),
                        'rationale':str(declared.get('rationale') or '根据材料提出的建模建议，尚待用户审核')[:1500],
                        'decision_version':policy.get('decision_version',0)}

            from .proposal_dedup import proposal_identity, compatibility_issue, merge_evidence
            marker=(current_project['revision'],current_project['proposal_generation'])
            cache_hit=context is not None and context.get('marker')==marker
            if cache_hit:
                snapshot=context['snapshot'];pending_index=context['pending_index'];base_catalog=context['baseline']
            else:
                snapshot=store.snapshot(pid,c);base_catalog=current_baseline(pid)
                pending=c.execute("SELECT id,kind,payload,evidence FROM proposals WHERE project_id=? AND status='pending'",(pid,)).fetchall()
                pending_index={proposal_identity(x['kind'],json.loads(x['payload'])):
                     {'id':x['id'],'payload':json.loads(x['payload']),'evidence':json.loads(x['evidence'])} for x in pending}
                if context is not None:
                    context.update(snapshot=snapshot,pending_index=pending_index,baseline=base_catalog)
            if len(items)>1000:
                raise HTTPException(422,'本次候选超过1000条；需分批处理，禁止静默截断')
            for item in items:
                kind=item.get('kind')
                if KIND_STAGE.get(kind)!=stage:
                    rejected.append('候选类型与当前阶段不匹配。')
                    if origin=='model':
                        record_gap(c,pid,stage,batch_number,str(kind or ''),item.get('payload',{}),item.get('evidence',[]),
                                   '候选类型与当前阶段不匹配',piece=(context or {}).get('piece'),cache_key=(context or {}).get('cache_key'))
                    continue
                try:
                    duplicate=duplicate_against_baseline(kind,item.get('payload',{}),base_catalog)
                    if duplicate:
                        reused.append(duplicate)
                        continue
                    evidence=validated_evidence(pid,item.get('evidence',[]),context=context)
                    prepared=prepare_payload(kind,item.get('payload',{}),evidence,snapshot)
                    payload=validate_payload(kind,prepared,snapshot)
                    if origin!='human':
                        scope_reason=modeling.payload_scope_reason(kind,payload,snapshot,policy,origin_for(item)['kind'])
                        if scope_reason:
                            omitted.append({'name':payload.get('name') or payload.get('predicate'), 'reason':scope_reason})
                            continue
                    if kind=='attribute' and base_catalog and (payload.get('datatype') not in ('string','integer','double','date','boolean')
                                                              or payload.get('multivalued')):
                        raise InvalidOntology('当前已绑定 OpenMind 分支，原生属性仅支持五种单值数据类型；请人工调整后再提交。')
                    # Remote inherited properties and relation codes need a second check
                    # after local schema normalization; never silently create duplicates.
                    duplicate=duplicate_against_baseline(kind,payload,base_catalog)
                    if duplicate:
                        reused.append(duplicate)
                        continue
                    if kind=='relation' and payload.get('predicate')=='subClassOf':
                        child=next((x for x in snapshot['entities'] if x['id']==payload.get('source_id')),None)
                        if child and child.get('origin')=='openmind':
                            raise InvalidOntology('不能通过新建本地 subClassOf 修改远端已有实体的父类；请走独立人工变更审批')
                    key=proposal_identity(kind,payload)
                    evidence=validated_evidence(pid,item.get('evidence',[]),context=context)
                    if key in pending_index:
                        previous=pending_index[key]
                        issue=compatibility_issue(kind,previous['payload'],payload)
                        if issue:
                            rejected.append(issue)
                            if origin=='model':
                                record_gap(c,pid,stage,batch_number,kind,payload,evidence,issue,
                                           piece=(context or {}).get('piece'),cache_key=(context or {}).get('cache_key'))
                        else:
                            merged=merge_evidence(previous['evidence'],evidence)
                            if merged!=previous['evidence']:
                                c.execute('UPDATE proposals SET evidence=? WHERE project_id=? AND id=?',
                                          (j(merged),pid,previous['id']))
                                previous['evidence']=merged
                            modeling.save_proposal_origin(c,pid,previous['id'],origin_for(item),policy['fingerprint'])
                            reused.append(f'{kind} 已合并同一候选的重复来源')
                        continue
                    eid=uid()
                    c.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (eid,pid,stage,kind,j(payload),j(evidence),origin,'pending',snapshot['project']['revision'],'',now(),None))
                    modeling.save_proposal_origin(c,pid,eid,origin_for(item),policy['fingerprint'])
                    pending_index[key]={'id':eid,'payload':payload,'evidence':evidence}
                    created.append(eid)
                except (InvalidOntology, OpenMindError) as e:
                    label=str(item.get('payload',{}).get('name') or item.get('payload',{}).get('predicate') or kind or '')[:80]
                    reason=f'{label or "未命名候选"}：{e}'
                    rejected.append(reason)
                    if origin=='model':
                        record_gap(c,pid,stage,batch_number,kind or '',item.get('payload',{}),item.get('evidence',[]),reason,
                                   piece=(context or {}).get('piece'),cache_key=(context or {}).get('cache_key'))
            if reused and origin=='human':
                store.record_action(c,pid,stage,'用户提出本体候选，系统检查指定 OpenMind 分支已有定义。',
                                    '候选与已有分支定义相同，建议复用：'+'；'.join(reused[:5])[:2200])
            if origin=='human' and created:
                name=str(items[0].get('payload',{}).get('name') or items[0].get('payload',{}).get('predicate') or '未命名')[:70]
                store.record_action(c,pid,stage,
                                    f'用户手动提交{kind}候选「{name}」，等待审核。',
                                    f'已添加待审核候选「{name}」；正式本体尚未修改。')
            c.execute('UPDATE projects SET proposal_generation=proposal_generation+1 WHERE id=?',(pid,))
            if context is not None:context['marker']=(marker[0],marker[1]+1)
            store.audit(c,pid,actor,'proposals_created',{'stage':stage,'origin':origin,'count':len(created),
                            'reused':reused,'omitted_by_user_scope':omitted[:10],'rejected':rejected[:8]})
        return {'created':created,'rejected':rejected,'reused':reused,'omitted_by_user_scope':omitted}

    def source_signature(pid, kind, stage, config):
        project=get_project(pid)
        # Content hashes and parse revisions replace repeated reading/hashing
        # megabytes of chunks. Reparse atomically increments content_revision.
        source_docs=store.many('SELECT id,sha256,content_revision FROM documents WHERE project_id=? ORDER BY created_at,id',(pid,))
        entities=(store.many('SELECT id,name FROM entities WHERE project_id=? ORDER BY id',(pid,))
                  if kind=='propose' and stage in ('PROPERTY','RELATION') else [])
        obj={'algorithm':'v143-semantic-contract','kind':kind,'stage':stage,
             'modeling_context':modeling.runtime(store,pid)['fingerprint'],
             'scope':project['scope'] if kind=='propose' else '',
             'model':config.get('model',''),'base_url':config.get('base_url',''),
             'prompt_version':config.get('prompt_version',''),
             'structured_output':config.get('structured_output') or os.getenv('LLM_STRUCTURED_OUTPUT','off'),
             'openmind_baseline':(baseline_row(pid) or {}).get('fingerprint',''),
             'documents':source_docs,'approved_entities':entities}
        return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def update_job(jid, **updates):
        if not updates:return
        updates['updated_at']=now()
        with store.tx() as c:
            c.execute('UPDATE workflow_jobs SET '+','.join(key+'=?' for key in updates)+' WHERE id=?',
                      tuple(updates.values())+(jid,))
            if updates.get('state') in ('succeeded','partial','failed','interrupted','queued'):
                attach_to_job_result(c,jid)

    def announce_job_progress(pid, jid, stage, phase, content, *, percent=None, eta_seconds=None, force=False):
        """Persist an evidence-grounded progress message for the right-side assistant.

        Bounded event history and phase/percentage/time throttling keep long
        documents from filling the conversation with near-identical messages.
        No estimates are invented before enough completed work is available.
        """
        from datetime import datetime as _dt
        content=' '.join(str(content).split())[:420]
        pct=None if percent is None else max(0,min(100,int(percent)))
        eta=None if eta_seconds is None else max(0,int(eta_seconds))
        with store.tx() as c:
            latest=c.execute("""SELECT phase,percent,created_at FROM job_progress_events
                WHERE job_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",(jid,)).fetchone()
            if latest and not force and latest['phase']==phase:
                last_pct=latest['percent']
                age=(_dt.now(timezone.utc)-_dt.fromisoformat(latest['created_at'])).total_seconds()
                if (pct is None or (last_pct is not None and pct-last_pct < 6)) and age < 12:
                    return False
            c.execute("""INSERT INTO job_progress_events
                (id,project_id,job_id,stage,phase,content,percent,eta_seconds,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uid(),pid,jid,stage,phase,content,pct,eta,_dt.now(timezone.utc).isoformat(timespec='microseconds')))
            c.execute('UPDATE workflow_jobs SET phase=?,detail=?,updated_at=? WHERE id=? AND project_id=?',
                      (phase,content,now(),jid,pid))
        return True

    app.state.announce_job_progress=announce_job_progress

    def piece_location(piece,documents):
        filename=next((d['filename'] for d in documents if d['id']==piece.get('document_id')),'当前文档')
        locator=str(piece.get('locator') or '').strip()
        # The text is a short source preview, not an LLM claim about findings.
        excerpt=' '.join(str(piece.get('content','')).split())[:65]
        return f'《{filename}》{locator or "当前片段"}：{excerpt}'

    def run_structured_stage(pid,jid,stage,signature,config,actor,documents,baseline,*,only_piece=None,force_retry=False):
        """Discover -> align -> persist for EACH completed unit, not all-or-nothing."""
        from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
        from contextvars import copy_context
        from time import perf_counter
        from .gap_state import record_gap
        from .progress_estimate import DiscoveryETA
        from .schema_catalog import build_attribute_catalog
        config=configured_model(pid,config)
        pieces=list(incremental.segments(documents)) if only_piece is None else [only_piece]
        total=len(pieces);seen_done=0;created=reused=gap_count=0
        failures=[];incomplete=set();question_ids=[];scope_omitted=0;basis_counts={'source_explicit':0,'inference':0}
        source_profile=material_profile(documents)
        reading_gaps=[]
        if only_piece is None:
            for document in source_profile['documents']:
                for warning in document['warnings']:
                    reading_gaps.append({'document_id':document['id'], 'filename':document['filename'],
                                         'warning':str(warning)})
            if reading_gaps:
                with store.tx() as c:
                    for problem in reading_gaps:
                        # Reading boundaries are source-level issues, not failed
                        # semantic chunks. Reparse the document to resolve them.
                        problem_id=record_gap(c,pid,stage,0,'source_coverage',problem,[],
                            problem['warning'],piece={'document_id':problem['document_id'],
                            'locator':'文档阅读边界','content_hash':None},cache_key=None)
                        question_ids.append(problem_id)
                gap_count+=len(reading_gaps)
                announce_job_progress(pid,jid,stage,'preparing',
                    f'材料有 {len(reading_gaps)} 处图片、版式或修订内容尚未充分核实。先处理已读到的文字；这些阅读边界保留为待人工核查项。',force=True)
        context={'known_docs':{d['id']:d for d in documents},'trusted_basis':True,
                 'policy_fingerprint':config.get('interpretation',{}).get('fingerprint','')}
        refs=build_attribute_catalog(documents)
        snapshot=store.snapshot(pid);snapshot_revision=snapshot['project']['revision']
        limit=max(1,min(8,int(os.getenv('EXTRACTION_CONCURRENCY','3'))))
        eta=DiscoveryETA(parallelism=limit)
        def ensure_source():
            p=get_project(pid)
            if p['stage']!=stage or not p['scope_confirmed']:
                raise ValueError('建模阶段或主题确认已变化，已保存候选保留，请重新开始')
            if source_signature(pid,'propose',stage,config)!=signature:
                raise ValueError('文档、分支或已确认实体发生变化，停止使用旧上下文')
        cancel=threading.Event()
        def load(piece):
            if cancel.is_set():raise ValueError('任务已停止，不再提交新模型请求')
            project=get_project(pid)
            if project['stage']!=stage or not project['scope_confirmed']:raise ValueError('构建阶段已改变')
            started=perf_counter()
            with store.tx():pass  # fence before spending an external model call
            with brain.using_model(config):
                clues,hit=incremental.cached_clues(store,piece,config,brain.joint_extract_segment,force_retry=force_retry)
            return clues,hit,perf_counter()-started
        announce_job_progress(pid,jid,stage,'discovering',
            f'正在处理 {total} 个来源单元；每个完成单元会立即生成可审核候选。单段失败不会丢弃其他结果。',percent=3,force=True)
        groups={}
        for i,piece in enumerate(pieces):
            groups.setdefault(incremental.clue_cache_key(piece,config),[]).append(i)
        def completions(pool):
            iterator=iter(groups.values());pending={}
            def submit():
                try:ids=next(iterator)
                except StopIteration:return
                pending[pool.submit(copy_context().run,load,pieces[ids[0]])]=ids
            for _ in range(min(limit*2,len(groups))):submit()
            try:
                while pending:
                    ready,_=wait(pending,return_when=FIRST_COMPLETED)
                    for future in ready:
                        ids=pending.pop(future)
                        yield future,ids
                        submit()
            finally:
                cancel.set()
                for future in pending:future.cancel()
        with ThreadPoolExecutor(max_workers=limit,thread_name_prefix='ontoflow-discovery') as pool:
            for future,ids in completions(pool):
                ensure_source()
                error=None;hit=False;elapsed=0
                try:clues,hit,elapsed=future.result()
                except LostLease:raise
                except Exception as exc:
                    error=(str(exc) or type(exc).__name__)[:500]
                    row=store.one('SELECT result FROM extraction_cache WHERE cache_key=?',
                        (incremental.clue_cache_key(pieces[ids[0]],config),))
                    clues=json.loads(row['result']) if row else {'entities':[],'attributes':[],'relations':[],
                         '_complete':False,'_pending':[],'_unresolved':[]}
                    failures.append({'batches':[i+1 for i in ids],'error':error})
                eta.observe(len(ids),elapsed,model_invoked=bool(clues.get('_coverage',{}).get('model_calls')),cache_hit=hit)
                for index in ids:
                    piece=pieces[index];batch_number=piece.get('_batch_number',index+1)
                    cache_key=incremental.clue_cache_key(piece,config)
                    project=get_project(pid)
                    if project['revision']!=snapshot_revision:
                        snapshot=store.snapshot(pid);snapshot_revision=project['revision']
                    scope_omitted+=clues.get('_intent_applied',{}).get('omitted_candidates',0)
                    items,gaps=incremental.align_clues(stage,clues,snapshot,piece,
                                                      baseline=baseline,catalog_reference=refs)
                    if error:gaps.append({'kind':'retryable_error','payload':{},'evidence':[],
                                          'reason':'该单元处理失败，其他单元继续：'+error})
                    for note in clues.get('_unresolved',[]):
                        gaps.append({'kind':note.get('category') or note.get('recovery') or 'discovery',
                            'payload':{'source_excerpt':note.get('source_excerpt',''),
                                       'recovery':note.get('recovery',clues.get('_state','awaiting_user'))},
                            'evidence':[],'reason':note.get('reason','提取完整性待核查')})
                    if clues.get('_pending') or clues.get('_retry_units'):
                        gaps.append({'kind':'retryable','payload':{'recovery':'retryable'},'evidence':[],
                                    'reason':'该来源单元尚有待处理内容；可定向重试，成功片段不会重新提取'})
                    if error or clues.get('_complete') is False:incomplete.add(index)
                    # Every independently valid fragment is available BEFORE
                    # all futures finish. Approval remains a separate human step.
                    context['piece']=piece;context['cache_key']=cache_key
                    output=add_proposals(pid,stage,items,'model',actor,batch_number=batch_number,context=context)
                    created+=len(output['created']);reused+=len(output.get('reused',[]))
                    for item in items:
                        basis=item.get('_basis',{}).get('kind','inference')
                        basis_counts[basis]=basis_counts.get(basis,0)+1
                    with store.tx() as c:
                        for gap in gaps:
                            question_ids.append(record_gap(c,pid,stage,batch_number,gap['kind'],gap['payload'],
                                gap['evidence'],gap['reason'],piece=piece,cache_key=cache_key))
                        c.execute('''INSERT INTO extraction_batch_audit
                           (project_id,stage,source_fingerprint,batch_number,proposed_count,
                            accepted_for_review,reused_count,gap_count,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,stage,source_fingerprint,batch_number)
                           DO UPDATE SET proposed_count=excluded.proposed_count,
                             accepted_for_review=excluded.accepted_for_review,reused_count=excluded.reused_count,
                             gap_count=excluded.gap_count,updated_at=excluded.updated_at''',
                           (pid,stage,signature,batch_number,len(items),len(output['created']),
                            len(output.get('reused',[])),len(gaps)+len(output['rejected']),now()))
                    gap_count+=len(gaps)+len(output['rejected']);seen_done+=1
                    update_job(jid,completed_units=seen_done,total_units=total,phase='aligning',
                        detail=f'已处理 {seen_done}/{total} 个来源单元，可审核 {created} 条，复用 {reused} 条，待核查 {gap_count} 项')
                    announce_job_progress(pid,jid,stage,'aligning',
                        f'已处理 {piece_location(piece,documents)}。当前可审核 {created} 条；线索依据分为原文明示 {basis_counts["source_explicit"]}、建模推断 {basis_counts["inference"]}；按用户范围暂不纳入 {scope_omitted} 项。复用 {reused} 条，未完成来源 {len(incomplete)} 个。',
                        percent=int(5+90*seen_done/max(1,total)),eta_seconds=eta.estimate(seen_done,total))
        ensure_source()
        status='partial' if incomplete or reading_gaps else 'succeeded'
        report={'created':created,'reused':reused,'gaps':gap_count,'incomplete_segments':len(incomplete),
                'failed_segments':failures,'coverage_complete':not incomplete and not reading_gaps,
                'source_parts_complete':not reading_gaps,'reading_boundaries':len(reading_gaps),
                'candidate_basis_counts':basis_counts,'omitted_by_user_scope':scope_omitted,
                'modeling_context_fingerprint':config.get('interpretation',{}).get('fingerprint',''),
                'processed_segments':seen_done,'total_segments':total,'gap_ids':question_ids[:100]}
        with store.tx() as c:
            if only_piece is None:
                # next_batch describes traversal, NOT proof of complete coverage.
                c.execute('''INSERT INTO proposal_progress VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,stage)
                    DO UPDATE SET source_fingerprint=excluded.source_fingerprint,next_batch=excluded.next_batch,
                    total_batches=excluded.total_batches,updated_at=excluded.updated_at''',
                    (pid,stage,signature,total,total,now()))
            c.execute('UPDATE workflow_jobs SET state=?,phase=?,completed_units=?,total_units=?,result=?,error=?,detail=?,updated_at=? WHERE id=?',
                 (status,status,total,total,j(report),'',
                  f'本轮处理结束：{created} 条待审核候选，{len(incomplete)} 个来源单元未完成，{gap_count} 项待协商',now(),jid))
            store.record_action(c,pid,stage,'本轮原文处理完成，准备人工审核。',
                f'已提供 {created} 条可审核候选；未完成来源 {len(incomplete)} 个、待协商项 {gap_count} 个。未完成不等于全部识别成功。')
            store.audit(c,pid,actor,'incremental_extraction_finished',report)
            attach_to_job_result(c,jid)
        announce_job_progress(pid,jid,stage,status,
            f'本轮文字处理结束，候选 {created} 条已可审核。{len(incomplete)} 个来源单元未完成，{len(reading_gaps)} 项文档阅读边界待核查；可在缺口列表重试、暂缓或记录决定。',
            percent=round(100*(total-len(incomplete))/max(1,total)),eta_seconds=0 if not incomplete else None,force=True)
        return report

    def run_background_job(pid, jid, job_kind, stage, signature, config, actor):
        """Each batch commits independently; retries resume the last committed cursor.

        Workers deliberately do not mutate the approved graph without approval.
        No fixed-duration request is held open while the user waits.
        """
        try:
            if job_kind=='review_all':
                from .review_all import run_review_all
                return run_review_all(store,pid,jid,stage,apply_proposal_decision,announce_job_progress)
            if job_kind=='gap_retry':
                gap=store.one('SELECT * FROM extraction_gaps WHERE id=? AND project_id=?',(signature,pid))
                if not gap or gap['status']!='retrying':raise ValueError('缺口不存在或未获重试授权')
                documents=docs(pid)
                piece=next((x for x in incremental.segments(documents) if x['document_id']==gap['document_id']
                            and x['ordinal']==gap['segment_ordinal'] and x['content_hash']==gap['content_hash']),None)
                if not piece:raise ValueError('来源片段已改变，定向重试中止')
                piece['_batch_number']=gap['batch_number']
                piece['_retry_gap_id']=gap['id']
                baseline=require_baseline(pid)
                report=run_structured_stage(pid,jid,stage,source_signature(pid,'propose',stage,config),config,actor,documents,baseline,
                                            only_piece=piece,force_retry=True)
                # Resolution is source-specific: if this exact issue persists,
                # keep it open. Other newly discovered questions remain separate.
                state='open' if (signature in report.get('gap_ids',[]) or not report['coverage_complete']) else 'resolved'
                with store.tx() as c:
                    c.execute('UPDATE extraction_gaps SET status=?,decided_at=?,decision_revision=decision_revision+1 WHERE id=? AND status=?',
                              (state,now(),signature,'retrying'))
                    c.execute('INSERT INTO gap_decisions VALUES(?,?,?,?,?,?,?,?,?)',
                              (uid(),signature,pid,'retry_result','单个来源单元重新校验',actor,gap['decision_revision']+1,j(report),now()))
                return report
            if job_kind=='parse':
                update_job(jid,state='running',phase='parsing',detail='解析文档结构、章节和表格')
                row=store.one('SELECT * FROM documents WHERE id=? AND project_id=?',(signature,pid))
                if not row:raise ValueError('待解析文档不存在')
                announce_job_progress(pid,jid,'UPLOAD','parsing',f'开始读取《{row["filename"]}》：正在解析文件结构、章节与表格；首次解析完成前暂无法精确估时。',percent=3,force=True)
                raw_file=Path(row['path'])
                if not raw_file.is_file():raise ValueError('原始文件不存在')
                parsed=extract(raw_file)
                with store.tx() as c:
                    c.execute('UPDATE documents SET content=?,chunks=?,warnings=?,content_revision=content_revision+1 WHERE id=? AND project_id=?',
                              (parsed['content'],j(parsed['chunks']),j(parsed['warnings']),row['id'],pid))
                    reading_state='partial' if parsed['warnings'] else 'succeeded'
                    reading_phase='partial' if parsed['warnings'] else 'complete'
                    detail=f'已读取 {len(parsed["chunks"])} 个文字片段；{len(parsed["warnings"])} 项格式/未读内容需核查，不代表语义完整。'
                    c.execute("UPDATE workflow_jobs SET state=?,phase=?,completed_units=1,total_units=1,detail=?,result=?,updated_at=? WHERE id=?",
                              (reading_state,reading_phase,detail,j({'reading_warnings':parsed['warnings'],
                                'parser_version':parsed.get('parser_version'),'text_units':len(parsed['chunks'])}),now(),jid))
                    store.record_action(c,pid,'UPLOAD',f'系统完成「{row["filename"]}」的文字读取。',detail)
                    attach_to_job_result(c,jid)
                incremental.persist_segments(store,store.one('SELECT * FROM documents WHERE id=?',(row['id'],)))
                announce_job_progress(pid,jid,'UPLOAD',reading_phase,f'《{row["filename"]}》：'+detail,percent=100,eta_seconds=0,force=True)
                return
            update_job(jid,state='running',phase='preparing',detail='正在准备文档与模型请求')
            announce_job_progress(pid,jid,stage or 'TOPIC','preparing',
                '正在核对文档、模型配置及 OpenMind 已有本体，准备开始读取原文。',percent=2,force=True)
            documents=docs(pid)
            if not documents:raise ValueError('文档已经被移除')
            baseline=require_baseline(pid)
            if job_kind=='summary':
                from time import perf_counter
                from .progress_estimate import DiscoveryETA
                summary_eta=DiscoveryETA(parallelism=1)
                batch_clock=[None]
                def on_progress(phase, done, total, detail):
                    if phase=='reading':
                        batch_clock[0]=perf_counter()
                    elif phase=='summarizing' and batch_clock[0] is not None:
                        cost=perf_counter()-batch_clock[0]
                        # Cached summary maps finish too quickly to count as
                        # remote inference samples. They still count as done.
                        summary_eta.observe(1,cost,model_invoked=cost>=0.5)
                        batch_clock[0]=None
                    updates={'phase':phase,'detail':detail}
                    if done is not None:updates['completed_units']=done
                    if total is not None:updates['total_units']=total
                    update_job(jid,**updates)
                    # Reading begins before the LLM request; finishing a batch
                    # reports actual model findings. No fabricated streaming.
                    pct=(int(8+78*done/total) if done is not None and total and phase in ('reading','summarizing')
                         else 90 if phase in ('merging','finalizing') else 5)
                    eta=summary_eta.estimate(done,total) if phase=='summarizing' and done is not None else None
                    announce_job_progress(pid,jid,'TOPIC',phase,detail,percent=pct,eta_seconds=eta,
                                          force=phase in ('merging','finalizing'))
                def read_summary_cache(key):
                    saved=store.one('SELECT result FROM summary_cache WHERE cache_key=?',(key,))
                    return json.loads(saved['result']) if saved else None
                def write_summary_cache(key,value):
                    with store.tx() as c:
                        c.execute('INSERT INTO summary_cache (cache_key,content_hash,model_name,result,created_at) VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING',
                                  (key,key,config.get('model',''),j(value),now()))
                with brain.using_model(config),brain.using_summary_cache(read_summary_cache,write_summary_cache):
                    result=brain.analyze(documents,on_progress=on_progress,baseline=baseline)
                if source_signature(pid,job_kind,stage,config)!=signature:
                    raise ValueError('本次摘要期间源文档或模型设置发生变化，结果已丢弃，请重新运行。')
                with store.tx() as c:
                    c.execute("UPDATE projects SET candidate_summary=?,candidate_scope=?,stage='TOPIC',scope_confirmed=0,updated_at=? WHERE id=?",
                              (result['summary'],result['scope'],now(),pid))
                    c.execute('UPDATE workflow_jobs SET state=?,phase=?,detail=?,result=?,updated_at=? WHERE id=?',
                              ('succeeded','complete','全文概要及建议主题已生成，等待人工确认',j({'coverage':result.get('coverage',{}),'understanding':result.get('understanding',{}),'questions':result.get('questions',[])}),now(),jid))
                    c.execute('INSERT INTO messages (id,project_id,stage,role,content,created_at,source) VALUES(?,?,?,?,?,?,?)',
                              (uid(),pid,'TOPIC','assistant','文档概要已生成，请人工核对主题与建模范围。',now(),'action'))
                    store.audit(c,pid,actor,'summary_job_succeeded',{'job_id':jid})
                    attach_to_job_result(c,jid)
                announce_job_progress(pid,jid,'TOPIC','complete','已生成可读取内容的概要和主题建议；请核对材料阅读边界、未读内容和本轮建模范围。',percent=100,eta_seconds=0,force=True)
            else:
                if not get_project(pid)['scope_confirmed'] or get_project(pid)['stage']!=stage:
                    raise ValueError('构建阶段已变化，请重新启动候选提取')
                if store.is_postgres or os.getenv('STRUCTURED_EXTRACTION')=='1' or config.get('interpretation',{}).get('decision_version',0)>0:
                    return run_structured_stage(pid,jid,stage,signature,config,actor,documents,baseline)
                previous=store.one('SELECT * FROM proposal_progress WHERE project_id=? AND stage=?',(pid,stage))
                cursor=previous['next_batch'] if previous and previous['source_fingerprint']==signature else 0
                # SQLite-only compatibility path. Production always takes the
                # fenced per-source streaming pipeline above.
                total=1 if not brain_conf_enabled(config) else len(brain._document_batches(documents,budget=3000))
                if cursor>=total:
                    update_job(jid,state='succeeded',phase='complete',completed_units=total,total_units=total,
                               detail='本阶段既有批次已遍历；请核查候选和缺口',result=j({'created':0,'rejected':0}))
                    return
                count=rejected=reused=cache_hits=0
                incomplete_piece_indices=set()
                update_job(jid,phase='extracting',completed_units=cursor,total_units=total,
                           detail=f'开始处理第 {cursor+1}/{total} 批')
                announce_job_progress(pid,jid,stage,'aligning',
                    '本轮文档线索发现已执行，正在与 OpenMind 及人工确认定义比对；对尚未覆盖完整的片段保留检查点与人工核查提示。',percent=69,force=True)
                while cursor<total:
                    if source_signature(pid,job_kind,stage,config)!=signature:
                        raise ValueError('文档、主题、已确认实体或模型设置发生变化，本次任务中断；已完成批次得到保留。')
                    with brain.using_model(config):
                        out=brain.proposal_batches(stage,documents,store.snapshot(pid),get_project(pid)['scope'],
                                                   start_batch=cursor,max_batches=1,baseline=baseline)
                    if out.get('cache_hit'):cache_hits+=1
                    added=add_proposals(pid,stage,out['items'],'model' if brain_conf_enabled(config) else 'rules',actor,batch_number=cursor+1)
                    cursor=out['next_batch'];total=out['total']
                    count+=len(added['created']); rejected+=len(added['rejected'])+out.get('alignment_gaps',0);reused+=len(added['reused'])
                    with store.tx() as c:
                        c.execute('''INSERT INTO extraction_batch_audit
                            (project_id,stage,source_fingerprint,batch_number,proposed_count,accepted_for_review,reused_count,gap_count,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,stage,source_fingerprint,batch_number)
                            DO UPDATE SET proposed_count=excluded.proposed_count,
                            accepted_for_review=excluded.accepted_for_review,
                            reused_count=excluded.reused_count,
                            gap_count=excluded.gap_count,updated_at=excluded.updated_at''',
                            (pid,stage,signature,cursor,len(out['items']),len(added['created']),len(added['reused']),len(added['rejected'])+out.get('alignment_gaps',0),now()))
                        c.execute('''INSERT INTO proposal_progress
                          (project_id,stage,source_fingerprint,next_batch,total_batches,updated_at)
                          VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,stage) DO UPDATE SET
                          source_fingerprint=excluded.source_fingerprint,next_batch=excluded.next_batch,
                          total_batches=excluded.total_batches,updated_at=excluded.updated_at''',
                          (pid,stage,signature,cursor,total,now()))
                        c.execute('''UPDATE workflow_jobs SET phase='extracting',completed_units=?,total_units=?,detail=?,result=?,updated_at=? WHERE id=?''',
                                  (cursor,total,f'已完成 {cursor}/{total} 批，新增 {count} 条待审候选、复用 {reused} 条、{rejected} 条校验缺口',
                                   j({'created':count,'rejected':rejected,'reused':reused,
                                      'cache_hits':cache_hits,'incomplete_segments':len(incomplete_piece_indices)}),now(),jid))
                    label=f'原文批次 {cursor}（SQLite 兼容测试模式）'
                    announce_job_progress(pid,jid,stage,'aligning',
                        f'正在核对 {label}。目前新增待审核候选 {count} 条，复用已有定义 {reused} 条，待核查缺口 {rejected} 条。',
                        percent=int(69+28*cursor/max(1,total)),eta_seconds=None)
                    if out['complete']:break
                with store.tx() as c:
                    c.execute('''UPDATE workflow_jobs SET state='succeeded',phase='complete',detail=?,updated_at=? WHERE id=?''',
                              (f'已处理 {total} 个片段，复用抽取缓存 {cache_hits} 次，新增 {count} 条候选；'
                               f'{len(incomplete_piece_indices)} 个片段待继续，{rejected} 个缺口需核对',now(),jid))
                    c.execute('INSERT INTO messages (id,project_id,stage,role,content,created_at,source) VALUES(?,?,?,?,?,?,?)',
                              (uid(),pid,stage,'assistant',
                               f'{stage} 阶段扫描已结束：已遍历 {total} 个片段，新增 {count} 条待审候选；'
                               f'{len(incomplete_piece_indices)} 个片段的线索尚未覆盖完整，可再次执行任务从检查点续跑；'
                               f'另有 {rejected} 个缺口需核查。遍历全文不代表百分之百识别。',now(),'action'))
                    store.audit(c,pid,actor,'extraction_job_succeeded',{'job_id':jid,'count':count,'reused':reused,'gaps':rejected})
                    attach_to_job_result(c,jid)
                if rejected:
                    latest_gaps=store.many('SELECT reason,payload FROM extraction_gaps WHERE project_id=? AND stage=? ORDER BY rowid DESC LIMIT 4',(pid,stage))
                    reasons=[]
                    for row in latest_gaps:
                        label=str(row['reason']).strip()
                        if label and label not in reasons:reasons.append(label[:90])
                    if reasons:
                        announce_job_progress(pid,jid,stage,'clarifying',
                            '需要你协商确认的建模问题：'+'；'.join(reasons[:3])+
                            '。可在右侧对话输入“帮我处理当前抽取缺口”，我会结合原文与已确认本体提出选项，所有修改仍需人工审批。',
                            percent=97,force=True)
                announce_job_progress(pid,jid,stage,'complete',
                    f'本轮任务结束：已处理 {total} 个片段，新增待审候选 {count} 条，复用 {reused} 条；'
                    f'{len(incomplete_piece_indices)} 个片段仍未覆盖完整（已存检查点，可再次启动续跑），'
                    f'共有 {rejected} 个需要核查的缺口。此进度表示本轮结束，不代表全文识别完整。',
                    percent=100,eta_seconds=0,force=True)
        except LostLease:
            raise
        except Exception as exc:
            # Retry transient external faults with bounded exponential backoff.
            # No partially completed discovery needs re-running: completed chunk
            # cache and stage progress are committed independently.
            err=(str(exc) or type(exc).__name__)[:900]
            if job_kind=='gap_retry':
                with store.tx() as c:
                    c.execute("UPDATE extraction_gaps SET status='open',decision_revision=decision_revision+1 WHERE id=? AND project_id=? AND status='retrying'",(signature,pid))
            transient=any(token in err.lower() for token in ('超时','连接失败','网络','http 429','http 502','http 503','http 504','timeout','connecterror'))
            attempt=(store.one('SELECT attempts FROM workflow_jobs WHERE id=?',(jid,)) if store.is_postgres else None)
            if job_kind!='gap_retry' and store.is_postgres and transient and attempt and attempt['attempts']<int(os.getenv('JOB_MAX_ATTEMPTS','3')):
                delay=min(300,10*(2**max(0,attempt['attempts']-1)))
                run_at=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(timespec='seconds')
                update_job(jid,state='queued',phase='retry_wait',error=err,next_run_at=run_at,
                           detail=f'外部服务暂不可用，{delay} 秒后自动重试；已完成批次及缓存保留')
                announce_job_progress(pid,jid,stage or 'TOPIC','retry_wait',
                    f'模型或网络请求暂时失败：{err[:100]}。计划约 {delay} 秒后重新调度，已保存的抽取缓存及候选不会清空。',force=True)
            else:
                update_job(jid,state='failed',phase='failed',error=err,
                           detail='任务失败；请查看具体原因，修复后可从上一已完成批次继续')
                announce_job_progress(pid,jid,stage or 'TOPIC','failed',
                    f'本次任务未完成：{err[:150]}。请检查模型或文档配置；已保存的结果不受影响。',force=True)

    def brain_conf_enabled(config):
        return bool(config.get('base_url') and config.get('model'))

    def run_job(pid,jid,job_kind,stage,signature,config,actor,*,lease=None):
        config=configured_model(pid,config)
        from .metrics import scope as metric_scope
        from .task_leases import heartbeat
        own_lease=lease is None
        if lease is None:
            if store.is_postgres:raise LostLease('生产任务必须由持有效领取代号的 Worker 执行')
            claimed=claim_job(store,uid(),job_id=jid)
            if not claimed:raise LostLease('任务未处于可领取状态')
            lease=Lease(jid,claimed['worker_id'],claimed['lease_generation'])
        if lease.job_id!=jid:raise LostLease('任务租约和目标 ID 不匹配')
        stop=threading.Event();watcher=None
        if own_lease:
            def renew_dev():
                while not stop.wait(20):
                    try:
                        if not heartbeat(store,lease):return
                    except Exception:return
            watcher=threading.Thread(target=renew_dev,daemon=True);watcher.start()
        try:
            with lease_scope(lease),metric_scope() as metrics:
                result=run_background_job(pid,jid,job_kind,stage,signature,config,actor)
                with store.tx() as c:
                    row=c.execute('SELECT result FROM workflow_jobs WHERE id=?',(jid,)).fetchone()
                    report=json.loads(row['result']) if row else {}
                    report['metrics']=metrics.public()
                    c.execute('UPDATE workflow_jobs SET result=? WHERE id=?',(j(report),jid))
                return result
        finally:
            stop.set()
            if watcher:watcher.join(timeout=2)
    app.state.run_job=run_job
    app.state.run_structured_stage=run_structured_stage
    app.state.source_signature=source_signature

    @app.post('/api/projects/{pid}/jobs/start',status_code=202,dependencies=[Depends(authorize)])
    def start_job(pid:str, request:Request, kind:str='summary'):
        project=get_project(pid)
        if kind not in ('summary','propose'):
            raise HTTPException(422,'kind 只允许 summary 或 propose')
        if not docs(pid):raise HTTPException(409,'请先上传文档。')
        if any(not d['content'].strip() for d in docs(pid)):
            raise HTTPException(409,'有文件正在解析或解析失败，请查看解析任务进度。')
        stage=project['stage'] if kind=='propose' else ''
        if kind=='propose' and (not project['scope_confirmed'] or stage not in KIND_STAGE.values()):
            raise HTTPException(409,'需要先确认主题并进入实体、属性或关系阶段。')
        require_baseline(pid)  # A connected branch must be loaded before any document-only generation.
        config=active_model_config()
        signature=source_signature(pid,kind,stage,config)
        total=(len(list(incremental.segments(docs(pid)))) if store.is_postgres and kind=='propose'
               else (1 if not brain_conf_enabled(config) else len(brain._document_batches(docs(pid),
                               budget=3000 if kind=='propose' else 10000))))
        jid=uid()
        with store.tx() as c:
            # Serialize enqueue operations for a workspace. A partial unique
            # index is a second line of defense against concurrent API callers.
            if store.is_postgres:
                locked=c.execute('SELECT id FROM projects WHERE id=? FOR UPDATE',(pid,)).fetchone()
                if locked is None:
                    raise HTTPException(404,'工作空间不存在')
            else:c.execute('BEGIN IMMEDIATE')
            if kind=='propose' and c.execute("SELECT id FROM workflow_jobs WHERE project_id=? AND job_kind='gap_retry' AND state IN ('queued','running') LIMIT 1",(pid,)).fetchone():
                raise HTTPException(409,'已有单元定向重试，请待其结束再发起整阶段提取')
            running=c.execute('''SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind=? AND stage=?
                      AND state IN ('queued','running') ORDER BY rowid DESC LIMIT 1''',(pid,kind,stage)).fetchone()
            if running:return {'job':dict(running),'already_running':True}
            c.execute('''INSERT INTO workflow_jobs
                (id,project_id,job_kind,stage,state,phase,completed_units,total_units,
                 detail,error,source_fingerprint,result,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (jid,pid,kind,stage,'queued','queued',0,total,'任务已提交，等待开始','',signature,'{}',now(),now()))
            label='文档概要与建议主题' if kind=='summary' else {'ENTITY':'实体','PROPERTY':'属性','RELATION':'关系'}[stage]+'候选'
            store.record_action(c,pid,project['stage'],f'用户点击生成{label}。',
                                f'{label}任务已提交，右侧本体协商助手将连续汇报阅读内容、阶段发现、进度和剩余时间估计。')
            store.audit(c,pid,authorize(request),'workflow_job_started',{'job_id':jid,'kind':kind,'stage':stage})
        if not store.is_postgres:
            thread=threading.Thread(target=run_job,args=(pid,jid,kind,stage,signature,config,authorize(request)),daemon=True,name=f'ontoflow-{kind}-{jid}')
            thread.start()
        return {'job':store.one('SELECT * FROM workflow_jobs WHERE id=?',(jid,)),'already_running':False}

    @app.get('/api/projects/{pid}/jobs/latest',dependencies=[Depends(authorize)])
    def latest_job(pid:str,kind:str='summary',stage:str=''):
        get_project(pid)
        if kind not in ('summary','propose'):raise HTTPException(422,'任务类型无效')
        row=store.one('''SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind=? AND stage=?
                      ORDER BY rowid DESC LIMIT 1''',(pid,kind,stage if kind=='propose' else ''))
        return {'job':row}

    @app.get('/api/projects/{pid}/gaps',dependencies=[Depends(authorize)])
    def list_gaps(pid:str,status:str='open',stage:str='',offset:int=0,limit:int=20):
        get_project(pid)
        if status not in ('open','resolved','deferred','retrying','all'):raise HTTPException(422,'无效缺口状态')
        where='project_id=?';params=[pid]
        if status!='all':where+=' AND status=?';params.append(status)
        if stage:
            if stage not in KIND_STAGE.values():raise HTTPException(422,'无效构建阶段')
            where+=' AND stage=?';params.append(stage)
        total=store.one('SELECT COUNT(*) AS n FROM extraction_gaps WHERE '+where,tuple(params))['n']
        rows=store.many('SELECT * FROM extraction_gaps WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                       (*params,max(1,min(limit,100)),max(0,offset)))
        for row in rows:
            row['payload']=json.loads(row['payload']);row['evidence']=json.loads(row['evidence'])
        return {'items':rows,'total':total,'offset':max(0,offset),'limit':max(1,min(limit,100))}

    @app.post('/api/projects/{pid}/gaps/{gid}/decision',dependencies=[Depends(authorize)])
    def decide_gap(pid:str,gid:str,body:GapDecisionIn,request:Request):
        actor=authorize(request);job_id=None;config=None
        if not body.note.strip():raise HTTPException(422,'请填写实际处置理由')
        if body.action not in ('resolve','defer','reopen','retry'):raise HTTPException(422,'缺口操作无效')
        with store.tx() as c:
            if not store.is_postgres:c.execute('BEGIN IMMEDIATE')
            p=c.execute('SELECT * FROM projects WHERE id=?'+(' FOR UPDATE' if store.is_postgres else ''),(pid,)).fetchone()
            if not p:raise HTTPException(404,'工作空间不存在')
            gap=c.execute('SELECT * FROM extraction_gaps WHERE id=? AND project_id=?'+(' FOR UPDATE' if store.is_postgres else ''),(gid,pid)).fetchone()
            if not gap:raise HTTPException(404,'缺口不存在')
            if gap['decision_revision']!=body.expected_version:raise HTTPException(409,'缺口已有新决定，请刷新后重试')
            if gap['status']=='retrying':raise HTTPException(409,'该缺口正在定向重试，请等待结果')
            state={'resolve':'resolved','defer':'deferred','reopen':'open','retry':'retrying'}[body.action]
            if body.action=='retry':
                if not p['scope_confirmed'] or p['stage']!=gap['stage']:
                    raise HTTPException(409,'需确认主题并切换到该缺口所在构建阶段')
                source=c.execute('SELECT * FROM documents WHERE id=? AND project_id=?',(gap['document_id'],pid)).fetchone()
                if not source:raise HTTPException(409,'缺少当前来源定位，请重新解析并生成该阶段候选')
                piece=next((x for x in incremental.segments([dict(source)]) if x['ordinal']==gap['segment_ordinal']),None)
                if not piece or piece['content_hash']!=gap['content_hash']:
                    raise HTTPException(409,'缺口来源已改变，拒绝用旧定位重算；请重新提取该阶段')
                active=c.execute("SELECT id FROM workflow_jobs WHERE project_id=? AND state IN ('queued','running') AND job_kind IN ('propose','gap_retry') LIMIT 1",(pid,)).fetchone()
                if active:raise HTTPException(409,'已有候选/缺口任务正在执行，请待其结束再定向重试')
                config=configured_model(pid);job_id=uid()
                c.execute('''INSERT INTO workflow_jobs(id,project_id,job_kind,stage,state,phase,source_fingerprint,
                      detail,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                      (job_id,pid,'gap_retry',gap['stage'],'queued','queued',gid,
                       '用户已明确要求只重试该来源单元',j({'gap_id':gid}),now(),now()))
            c.execute('''UPDATE extraction_gaps SET status=?,decision=?,decided_at=?,decision_revision=decision_revision+1,
                      resolved_by=? WHERE id=?''',(state,body.note.strip(),now(),actor,gid))
            c.execute('INSERT INTO gap_decisions VALUES(?,?,?,?,?,?,?, ?,?)',
                      (uid(),gid,pid,body.action,body.note.strip(),actor,gap['decision_revision']+1,j({'job_id':job_id,'status':state}),now()))
            store.record_action(c,pid,gap['stage'],f'用户对缺口执行 {body.action}：{body.note.strip()[:400]}',
                 '已保存建模决定；不会未经审批修改正式本体。'+('仅重算对应来源单元，其他片段缓存保持不变。' if job_id else '可随时重新打开该问题。'))
            store.audit(c,pid,actor,'gap_decision',{'gap_id':gid,'action':body.action,'status':state,'job_id':job_id})
        if job_id and not store.is_postgres:
            threading.Thread(target=run_job,args=(pid,job_id,'gap_retry',gap['stage'],gid,config,actor),daemon=True).start()
        return {'gap_id':gid,'status':state,'decision_version':body.expected_version+1,'job_id':job_id,
                'note':'人工解决表示记录处置结论，不等同于自动通过未验证的抽取或直接写入远端'}

    @app.get('/api/projects/{pid}/coverage',dependencies=[Depends(authorize)])
    def extraction_coverage(pid:str,stage:str='ENTITY',batch:int|None=None):
        p=get_project(pid)
        if stage not in KIND_STAGE.values():raise HTTPException(422,'stage 无效')
        config=configured_model(pid)
        signature=source_signature(pid,'propose',stage,config)
        pieces=list(incremental.segments(docs(pid))) if store.is_postgres else []
        texts=([f'【文档 {x["document_id"]} · {x["locator"]}】{x["content"]}' for x in pieces] if store.is_postgres
               else brain._document_batches(docs(pid),budget=3000)
               if brain_conf_enabled(config) else
               ['\n'.join(d['content'] for d in docs(pid))])
        rows=store.many('''SELECT batch_number,proposed_count,accepted_for_review,reused_count,gap_count,updated_at
                FROM extraction_batch_audit WHERE project_id=? AND stage=? AND source_fingerprint=?
                ORDER BY batch_number''',(pid,stage,signature))
        indexed={x['batch_number']:x for x in rows}
        discovery={}
        if store.is_postgres and pieces:
            cache_keys=[incremental.clue_cache_key(piece,config) for piece in pieces]
            for ix in range(0,len(cache_keys),200):
                batch_keys=cache_keys[ix:ix+200]
                placeholders=','.join('?' for _ in batch_keys)
                for record in store.many(f'SELECT cache_key,result FROM extraction_cache WHERE cache_key IN ({placeholders})',tuple(batch_keys)):
                    value=json.loads(record['result'])
                    discovery[record['cache_key']]={
                        'status':'partial' if value.get('_complete') is False else 'complete',
                        'coverage':value.get('_coverage',{}),
                        'unresolved_count':len(value.get('_unresolved',[])),
                        'pending_units':len(value.get('_pending',[]))}
        def discover_at(index):
            if not pieces:return {'status':'not_applicable'}
            return discovery.get(incremental.clue_cache_key(pieces[index],config),{'status':'unprocessed'})
        if batch is not None:
            if batch<1 or batch>len(texts):raise HTTPException(404,'文档批次不存在')
            return {'stage':stage,'batch':batch,'total':len(texts),'text':texts[batch-1],
                    'audit':indexed.get(batch),'discovery':discover_at(batch-1),
                    'notice':'原文片段与证据覆盖情况；模型结束不等于识别完整。'}
        results=[]
        for index,text in enumerate(texts,1):
            a=indexed.get(index)
            results.append({'batch':index,'characters':len(text),'excerpt':text[:220],
                            'processed':bool(a), 'proposed':a['proposed_count'] if a else None,
                            'pending_created':a['accepted_for_review'] if a else None,
                            'reused':a['reused_count'] if a else None,
                            'gaps':a['gap_count'] if a else None,
                            'discovery':discover_at(index-1)})
        overview={'complete':sum(1 for b in results if b['discovery']['status']=='complete'),
                  'partial':sum(1 for b in results if b['discovery']['status']=='partial'),
                  'unprocessed':sum(1 for b in results if b['discovery']['status']=='unprocessed')}
        return {'stage':stage,'total':len(texts),'processed':len(indexed),
                'discovery_coverage':overview,'batches':results,
                'notice':'逐批文本扫描与候选计数，不构成召回率证明；请核对空结果和缺口批次。'}

    @app.get('/api/projects/{pid}/jobs/{jid}',dependencies=[Depends(authorize)])
    def job_status(pid:str,jid:str,since:str=''):
        get_project(pid)
        row=store.one('SELECT * FROM workflow_jobs WHERE id=? AND project_id=?',(jid,pid))
        if not row:raise HTTPException(404,'任务不存在')
        if since:
            if len(since)>48:raise HTTPException(422,'进度游标格式无效')
            events=store.many('''SELECT * FROM job_progress_events WHERE project_id=? AND job_id=? AND created_at>?
                ORDER BY created_at ASC,id ASC LIMIT 80''',(pid,jid,since))
        else:
            events=list(reversed(store.many('''SELECT * FROM job_progress_events WHERE project_id=? AND job_id=?
                ORDER BY created_at DESC,id DESC LIMIT 40''',(pid,jid))))
        return {'job':row,'assistant_events':events}


    @app.exception_handler(OpenMindError)
    async def handle_openmind_error(_,exc):
        return Response(json.dumps({'detail':str(exc)},ensure_ascii=False),status_code=422,media_type='application/json')

    @app.exception_handler(InvalidOntology)
    async def handle_invalid(_,exc):
        return Response(json.dumps({'detail':str(exc)},ensure_ascii=False),status_code=422,media_type='application/json')

    @app.get('/')
    def index():
        return FileResponse(Path(__file__).resolve().parent.parent/'static'/'index.html')

    @app.get('/api/health')
    def health():
        try:
            public=model_settings.public()
            mode=public['mode']
        except ModelSettingsError:
            mode='CONFIG_ERROR'
        return {'ok':True,'engine':mode,'version':VERSION,'build_id':build_identity(),
                'demo_mode':demo_enabled(),'connection_policy':connection_policy()}

    @app.get('/api/ready')
    def readiness():
        try:
            store.one('SELECT 1 AS ok')
            return {'ready':True,'database':'postgresql' if store.is_postgres else 'sqlite-development'}
        except Exception:
            raise HTTPException(503,'数据库暂不可用')

    @app.get('/api/system/status',dependencies=[Depends(authorize)])
    def system_status():
        jobs=store.many('SELECT state,COUNT(*) AS n FROM workflow_jobs GROUP BY state')
        status={'database':'postgresql' if store.is_postgres else 'sqlite-development',
                'jobs':{r['state']:r['n'] for r in jobs}}
        if store.is_postgres:
            status['worker_heartbeats']=store.many('SELECT worker_id,updated_at FROM worker_heartbeats ORDER BY updated_at DESC LIMIT 8')
        return status

    @app.get('/api/settings/model',dependencies=[Depends(authorize)])
    def get_model_settings():
        return model_settings.public()

    @app.put('/api/settings/model',dependencies=[Depends(authorize_model_admin)])
    def update_model_settings(body:ModelSettingsIn):
        try:
            return model_settings.save(body.base_url,body.model,body.api_key,body.clear_api_key)
        except ModelSettingsError as e:
            raise HTTPException(422,str(e)) from e

    @app.delete('/api/settings/model',dependencies=[Depends(authorize_model_admin)])
    def reset_model_settings():
        return model_settings.reset()

    @app.post('/api/settings/model/test',dependencies=[Depends(authorize_model_admin)])
    def test_model_settings():
        config=active_model_config()
        try:
            validate_endpoint(config['base_url'])
            return brain.test_connection(config)
        except ModelSettingsError as e:
            raise HTTPException(422,str(e)) from e
        except ValueError as e:
            raise HTTPException(400,str(e)) from e
        except RuntimeError as e:
            raise HTTPException(502,str(e)) from e

    @app.get('/api/settings/openmind',dependencies=[Depends(authorize)])
    def get_openmind_settings():
        return openmind_settings.public()

    @app.put('/api/settings/openmind',dependencies=[Depends(openmind_admin)])
    def update_openmind_settings(body:OpenMindSettingsIn,request:Request):
        try:
            result=openmind_settings.save(body.base_url,body.access_token,body.allowed_hosts,
                                          body.clear_access_token,body.new_admin_token,body.clear_admin_token,
                                          body.auth_mode,body.username,body.password,body.clear_password)
        except (OpenMindError,ModelSettingsError) as e:
            raise HTTPException(422,str(e)) from e
        # This is a configuration operation, not a write to OpenMind.
        return result

    @app.delete('/api/settings/openmind',dependencies=[Depends(openmind_admin)])
    def reset_openmind_settings():
        try:return openmind_settings.reset()
        except OpenMindError as e:raise HTTPException(422,str(e)) from e

    @app.get('/api/settings/openmind/domains',dependencies=[Depends(openmind_admin)])
    def get_openmind_domains():
        # Access is scoped to the configured OpenMind identity. The endpoint
        # deliberately excludes credentials and any unselected catalog payload.
        client=make_openmind_client()
        try:
            try:return {'items':visible_domains(client)}
            except OpenMindError as exc:raise HTTPException(502,'OpenMind 领域目录读取失败：'+str(exc)) from exc
        finally:client.close()

    @app.post('/api/settings/openmind/test',dependencies=[Depends(openmind_admin)])
    def test_openmind_connection():
        # No project binding is needed to verify the credentials and service.
        try:
            client=make_openmind_client()
            try:
                domains=client.domains()
                if not isinstance(domains,dict) or not isinstance(domains.get('items'),list):
                    raise OpenMindError('OpenMind /api/v1/domains 未返回有效领域目录')
                account=client.session_account() if hasattr(client,'session_account') else None
                return {'connected':True,'visible_domain_count':len(domains['items']),
                        'read_only':True,'endpoint':openmind_settings.public()['base_url'],
                        'auth_mode':openmind_settings.public()['auth_mode'],
                        'account':({'username':account.get('username'),
                                    'displayName':account.get('displayName')} if account else None)}
            finally:client.close()
        except (OpenMindError,ModelSettingsError) as e:
            raise HTTPException(502,str(e)) from e

    @app.get('/api/projects/{pid}/openmind/status',dependencies=[Depends(authorize)])
    def openmind_status(pid:str):
        binding=openmind_binding(pid)
        mapped=store.many('SELECT kind,local_id,remote_id,code,lock_version,updated_at FROM openmind_mappings WHERE project_id=?',(pid,))
        runs=store.many('SELECT id,revision,status,result,created_at,updated_at FROM openmind_sync_runs WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 10',(pid,))
        return {'baseline':baseline_public(baseline_row(pid)) if current_baseline(pid) is not None else None,
                'configured':openmind_settings.public()['configured'],
                'endpoint':openmind_settings.public()['base_url'], 'branch':binding,
                'mappings':mapped,'runs':runs,
                'note':'定义保存 != 资源绑定 != 上线；目标鉴权使用服务端 OpenMind Bearer。'}

    @app.put('/api/projects/{pid}/openmind/binding',dependencies=[Depends(openmind_admin)])
    def bind_openmind(pid:str,body:OpenMindBindingIn,request:Request):
        get_project(pid)
        domain=uuid_value(body.domain_id,'domainId');branch=uuid_value(body.branch_id,'branchId')
        existing=openmind_binding(pid)
        if existing and (existing['domain_id']!=domain or existing['branch_id']!=branch):
            if baseline_row(pid):
                raise HTTPException(409,'已建立旧分支本体基线，为避免混合不同分支的实体 ID，切换分支请新建独立本体项目。')
            if store.one('SELECT 1 FROM openmind_mappings WHERE project_id=?',(pid,)):
                raise HTTPException(409,'已有远端对象映射，不允许重新绑定另一分支。请新建独立构建项目。')
        with store.tx() as c:
            c.execute('INSERT INTO openmind_bindings VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET domain_id=excluded.domain_id,branch_id=excluded.branch_id,updated_at=excluded.updated_at',(pid,domain,branch,now()))
            store.audit(c,pid,authorize(request),'openmind_bound',{'domainId':domain,'branchId':branch})
        try:
            loaded=refresh_baseline(pid,authorize(request))
        except OpenMindError as exc:
            raise HTTPException(502,'分支已绑定，但拉取现有实体/属性/关系失败；必须先修复连接并刷新基线，不能直接按文档构建：'+str(exc)) from exc
        return {**openmind_status(pid),'baseline_loaded':loaded}

    @app.post('/api/projects/{pid}/openmind/baseline/refresh',dependencies=[Depends(openmind_admin)])
    def refresh_existing_openmind(pid:str,request:Request):
        return {'baseline':refresh_baseline(pid,authorize(request))}

    @app.get('/api/projects/{pid}/openmind/baseline',dependencies=[Depends(authorize)])
    def read_existing_openmind(pid:str,kind:str='entity',q:str='',offset:int=0,limit:int=50):
        catalog=require_baseline(pid)
        if kind not in ('entity','attribute','relation'):
            raise HTTPException(422,'kind 必须为 entity/attribute/relation')
        rows=catalog[{'entity':'entities','attribute':'attributes','relation':'relations'}[kind]]
        if q.strip():rows=[x for x in rows if q.casefold() in (str(x.get('name',''))+' '+str(x.get('code',''))).casefold()]
        start=max(offset,0);size=max(1,min(limit,100))
        return {'kind':kind,'total':len(rows),'offset':start,'limit':size,
                'items':rows[start:start+size], 'baseline':baseline_public(baseline_row(pid))}

    @app.put('/api/projects/{pid}/openmind/code',dependencies=[Depends(openmind_admin)])
    def set_openmind_code(pid:str,body:OpenMindCodeIn,request:Request):
        get_project(pid)
        if body.kind not in ('entity','attribute','relation') or not CODE.fullmatch(body.code) or len(body.code)>(64 if body.kind=='relation' else 255):
            raise HTTPException(422,'CODE 必须是小写字母开头的英文/数字/下划线，关系不超过64字符。')
        field={'entity':'entities','attribute':'attributes','relation':'relations'}[body.kind]
        if not any(r['id']==body.local_id for r in store.snapshot(pid)[field]):raise HTTPException(404,'对象不存在。')
        if store.one('SELECT 1 FROM openmind_mappings WHERE project_id=? AND kind=? AND local_id=?',(pid,body.kind,body.local_id)):
            raise HTTPException(409,'已同步对象 CODE 不允许直接覆盖；请在目标系统做显式迁移。')
        with store.tx() as c:
            snap=store.snapshot(pid,c)
            obj=next(x for x in snap[field] if x['id']==body.local_id)
            changed={**obj,'code':body.code,'operation':'update'}
            changed['record_id' if body.kind=='relation' else 'target_id']=body.local_id
            reason=assignment_error(body.kind,changed,json.loads(obj.get('evidence') or '[]'),snap)
            if code_error(body.kind,body.code) or reason:raise HTTPException(422,code_error(body.kind,body.code) or reason)
            c.execute('INSERT INTO openmind_codes VALUES(?,?,?,?) ON CONFLICT(project_id,kind,local_id) DO UPDATE SET code=excluded.code',(pid,body.kind,body.local_id,body.code))
            store.revision(c,pid)
            store.audit(c,pid,authorize(request),'code_changed',{'kind':body.kind,'local_id':body.local_id,'code':body.code})
        return {'kind':body.kind,'local_id':body.local_id,'code':body.code,'requires_republish':True}

    @app.get('/api/projects/{pid}/openmind/plan',dependencies=[Depends(authorize)])
    def get_openmind_plan(pid:str,remote:bool=False):
        if not remote:return openmind_preview(pid)
        try:
            client=make_openmind_client()
            try:
                binding=openmind_binding(pid)
                if not binding: raise HTTPException(409,'请先绑定 OpenMind 分支。')
                catalog=client.catalog(binding['branch_id'])
                return openmind_preview(pid,catalog)
            finally:client.close()
        except OpenMindError as e:raise HTTPException(502,str(e)) from e

    @app.post('/api/projects/{pid}/openmind/test',dependencies=[Depends(openmind_admin)])
    def test_openmind(pid:str):
        binding=openmind_binding(pid)
        if not binding:raise HTTPException(409,'请先绑定 OpenMind 分支。')
        try:
            client=make_openmind_client()
            try:
                catalog=client.catalog(binding['branch_id']);parse_catalog(catalog)
                save_baseline(pid,catalog,'test_connection')
                types=client.types()
                return {'connected':True,'catalog_readable':True,'attribute_types_readable':bool(types),
                        'branch_id':binding['branch_id'],'read_only':True}
            finally:client.close()
        except OpenMindError as e:raise HTTPException(502,str(e)) from e

    @app.post('/api/projects/{pid}/openmind/sync',dependencies=[Depends(openmind_admin)])
    def sync_openmind(pid:str,body:OpenMindSyncIn,request:Request):
        if body.confirmation!='CONFIRM_OPENMIND_WRITE':
            raise HTTPException(400,'必须输入 CONFIRM_OPENMIND_WRITE 才允许远端写入。')
        snap,release=openmind_snapshot(pid)
        if body.expected_revision!=snap['project']['revision']:
            raise HTTPException(409,'本地修订变化，请重新预览并审批。')
        binding=openmind_binding(pid)
        if not binding:raise HTTPException(409,'请先绑定 OpenMind 分支')
        actor=authorize(request)
        session=SyncSession(store,pid,openmind_endpoint_hash(),binding['branch_id'],body.expected_revision,actor)
        client=None;status='partial_or_failed';details={}
        try:
            session.acquire()
            client=make_openmind_client()
            catalog=session.catalog(client)
            cached=baseline_row(pid)
            if not cached or cached['branch_id']!=binding['branch_id'] or cached['endpoint_hash']!=openmind_endpoint_hash():
                raise HTTPException(409,'缺少当前连接对应的基线，请先读取目录')
            # First reconcile only previously approved, journaled writes. This
            # can repair the local mapping after POST committed but timed out.
            try:own_only=session.reconcile(json.loads(cached['catalog_json']),catalog)
            except OpenMindError as exc:raise HTTPException(409,str(exc)) from exc
            if own_only and cached['fingerprint']!=baseline_hash(catalog):
                save_baseline(pid,catalog,actor,own_write=True)
            plan=openmind_preview(pid,catalog)
            if not plan['ready']:raise HTTPException(422,{'blockers':plan['blockers'],'warnings':plan['warnings']})
            final=session.execute(client,plan,catalog)
            session.assert_revision()
            if client.graph(binding['branch_id']) is None:raise OpenMindError('图回读为空，定义可能已保存但核验未完成')
            save_baseline(pid,final,actor,own_write=True)
            status='definitions_saved'
            details={'run_id':session.run_id,'status':status,'steps_done':session.sent,
                'catalog_readback':True,'graph_readable':True,'reconciled_operations':session.reconciled,
                'http_writes':session.writes,'catalog_reads':session.catalog_reads,
                'note':'仅定义保存；不代表资源绑定、执行就绪或上线'}
            return details
        except SyncBusy as exc:
            raise HTTPException(409,str(exc)) from exc
        except OpenMindError as exc:
            details={'error':str(exc),'partial_steps':session.sent,'run_id':session.run_id,
                     'retry':'同一修订重试会先按稳定 UUID 和完整请求语义对账；不会盲目重放 POST'}
            raise HTTPException(502,details) from exc
        finally:
            if client:client.close()
            session.release(status,details)

    @app.get('/api/projects/{pid}/openmind/operations',dependencies=[Depends(openmind_admin)])
    def list_sync_operations(pid:str):
        get_project(pid)
        rows=store.many('''SELECT id,run_id,revision,kind,local_ids,remote_id,code,action,state,
              attempts,error,created_at,updated_at FROM openmind_operations
              WHERE project_id=? ORDER BY updated_at DESC,id DESC LIMIT 100''',(pid,))
        return {'items':rows,'note':'不返回任何连接密钥；操作载荷保存在后端用于完整语义对账'}

    def scoped_sync_operation(pid,opid):
        binding=openmind_binding(pid)
        if not binding:raise HTTPException(409,'请先绑定 OpenMind 分支')
        op=store.one('SELECT * FROM openmind_operations WHERE id=? AND project_id=?',(opid,pid))
        if not op:raise HTTPException(404,'写入操作不存在')
        if op['endpoint_hash']!=openmind_endpoint_hash() or op['branch_id']!=binding['branch_id']:
            raise HTTPException(409,'此日志属于其他服务账号或分支，禁止在当前连接下处置')
        latest=store.one("""SELECT id FROM openmind_operations WHERE project_id=? AND endpoint_hash=? AND branch_id=?
             AND kind=? AND remote_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
             (pid,op['endpoint_hash'],op['branch_id'],op['kind'],op['remote_id']))
        if not latest or latest['id']!=opid:raise HTTPException(409,'已有更晚操作，请查看最新写入日志')
        return op,binding

    @app.post('/api/projects/{pid}/openmind/conflicts/check',dependencies=[Depends(openmind_admin)])
    def check_sync_conflicts(pid:str,request:Request):
        # POST because reconciliation updates local operation states/mappings;
        # the remote calls are GET-only. No approved request is sent again.
        from .sync_conflicts import operation_view,PLURAL,UNRESOLVED
        project=get_project(pid);binding=openmind_binding(pid)
        if not binding:raise HTTPException(409,'请先绑定 OpenMind 分支')
        actor=authorize(request);client=None;status='readback_failed';notes=[]
        session=SyncSession(store,pid,openmind_endpoint_hash(),binding['branch_id'],project['revision'],actor)
        try:
            session.acquire();client=make_openmind_client();current=session.catalog(client)
            cached=current_baseline(pid)
            if cached is None:raise HTTPException(409,'没有匹配当前连接的基线，不能处理其他分支的冲突')
            try:session.reconcile(cached,current,allow_external=True)
            except OpenMindError as exc:
                # Return field differences instead of another opaque toast. It
                # is correct for true conflicts to remain frozen after a check.
                notes.append(str(exc))
            index=parse_catalog(current)
            ops=store.many("""SELECT * FROM openmind_operations WHERE project_id=? AND endpoint_hash=? AND branch_id=?
                ORDER BY created_at DESC,id DESC""",(pid,session.endpoint,session.branch))
            seen=set();items=[]
            for op in ops:
                key=(op['kind'],op['remote_id'])
                if key in seen:continue
                seen.add(key)
                if op['state'] not in UNRESOLVED:continue
                items.append(operation_view(op,index[op['kind']].get(op['remote_id'])))
            status='conflicts_remain' if items else 'readback_resolved'
            with session.transaction() as c:
                store.audit(c,pid,actor,'sync_conflict_readback',{'remaining':len(items),'reconciled':session.reconciled,'remote_writes':0})
            return {'items':items,'remaining':len(items),'reconciled':session.reconciled,
                    'revision':get_project(pid)['revision'],'remote_writes':0,'notes':notes,
                    'next_step':'逐项核对差异，不会自动覆盖远端' if items else '没有待处理写入冲突；可刷新基线，再预览写入计划'}
        finally:
            if client:client.close()
            session.release(status,{'notes':notes,'remote_writes':0})

    @app.post('/api/projects/{pid}/openmind/operations/{opid}/return-to-review',dependencies=[Depends(openmind_admin)])
    def return_sync_to_review(pid:str,opid:str,body:SyncConflictIn,request:Request):
        from .sync_conflicts import observation_hash
        if body.confirmation!='KEEP_REMOTE_AND_REVIEW_AGAIN' or not body.note.strip():
            raise HTTPException(422,'请明确确认保留远端、撤销旧写入授权，并填写核对意见')
        op,binding=scoped_sync_operation(pid,opid)
        if op['state']!='CONFLICT':raise HTTPException(409,'此操作已不处于冲突状态，请重新核对')
        actor=authorize(request);client=None;status='resolution_failed'
        session=SyncSession(store,pid,openmind_endpoint_hash(),binding['branch_id'],body.expected_revision,actor)
        try:
            session.acquire();client=make_openmind_client();current=session.catalog(client)
            actual=parse_catalog(current)[op['kind']].get(op['remote_id'])
            # No force flags: changing code/UUID/scope is an identity collision,
            # not a semantic conflict this lightweight editor can settle.
            if actual is not None and (actual.get('code')!=op['code'] or str(actual.get('id'))!=op['remote_id']
                  or actual.get('branchId',op['branch_id'])!=op['branch_id']):
                raise HTTPException(409,'远端 UUID/CODE/分支身份冲突，不能解除；请在 OpenMind 中核查对象身份')
            with session.transaction() as c:
                if store.is_postgres:
                    p=c.execute('SELECT revision,stage FROM projects WHERE id=? FOR UPDATE',(pid,)).fetchone()
                else:p=c.execute('SELECT revision,stage FROM projects WHERE id=?',(pid,)).fetchone()
                row=c.execute('SELECT * FROM openmind_operations WHERE id=? AND project_id=?',(opid,pid)).fetchone()
                if not p or p['revision']!=body.expected_revision:raise HTTPException(409,'本地本体版本已变，请重新查看差异')
                if not row or row['state']!='CONFLICT' or observation_hash(row,actual)!=body.observation_hash:
                    raise HTTPException(409,'远端内容、版本或冲突记录已变化；未解锁，请重新回读比较')
                if openmind_endpoint_hash()!=session.endpoint:raise HTTPException(409,'服务连接已改变，请重新核对')
                # Mapping records identity only. The old desired payload remains
                # in the journal and is NOT changed into a false confirmation.
                for local_id in json.loads(row['local_ids']):
                    local_table={'entity':'entities','attribute':'attributes','relation':'relations'}[row['kind']]
                    if not c.execute(f'SELECT id FROM {local_table} WHERE project_id=? AND id=?',(pid,local_id)).fetchone():
                        raise HTTPException(409,'本次日志关联的本地对象已删除或替换，不能自动恢复映射；请人工核查')
                if actual is not None:session._mapping(c,row,actual)
                c.execute("UPDATE openmind_operations SET state='REVIEW_REQUIRED',error=?,updated_at=? WHERE id=?",
                          ('人工要求重新审核：'+body.note[:800],now(),opid))
                store.revision(c,pid)  # invalidates every previously published approval snapshot
                store.record_action(c,pid,p['stage'],f'用户保留 OpenMind 当前对象并撤销冲突操作 {opid} 的旧写入授权。',
                    '远端内容未改动。本地草稿未自动改写；请处理剩余冲突、刷新基线、比较并重新发布本地版本，之后才可再次审批同步。')
                store.audit(c,pid,actor,'sync_conflict_returned_to_review',{'operation_id':opid,
                    'note':body.note,'observation_hash':body.observation_hash,'remote_writes':0})
            status='review_required'
            return {'state':'REVIEW_REQUIRED','remote_writes':0,'revision':get_project(pid)['revision'],
                    'requires_baseline_refresh':True,'requires_republish':True,
                    'message':'已保留远端并撤销旧写入授权；不是同步成功，也没有自动再次写入。请刷新基线、检查草稿后重新发布和预览。'}
        finally:
            if client:client.close()
            session.release(status,{'operation_id':opid,'remote_writes':0})

    @app.get('/api/formats',dependencies=[Depends(authorize)])
    def formats():
        return capabilities()

    @app.post('/api/projects',dependencies=[Depends(authorize)])
    def create_project(body:ProjectIn,request:Request):
        actor=authorize(request)
        conf=openmind_settings.public()
        remote_enabled=conf['configured']
        if remote_enabled and (not body.domain_id or not body.branch_id):
            raise HTTPException(422,'OpenMind 已配置，创建工作空间时必须先选择有权限的领域和分支，不允许退回空白本体')
        if not remote_enabled and (body.domain_id or body.branch_id):
            raise HTTPException(422,'OpenMind 尚未连接，不能绑定分支；请先完成服务认证')
        pid=uid()
        if not remote_enabled:
            with store.tx() as c:
                c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',(pid,body.name.strip(),now(),now()))
                store.audit(c,pid,actor,'project_created',{'name':body.name,'mode':'offline'})
            return project_view(pid)
        openmind_admin(request)
        domain_id=uuid_value(body.domain_id,'domainId')
        branch_id=uuid_value(body.branch_id,'branchId')
        client=make_openmind_client()
        try:
            domains=visible_domains(client)
            domain=next((x for x in domains if x['id']==domain_id),None)
            if not domain:raise HTTPException(403,'指定领域不在当前 OpenMind 接入账号可访问的领域中')
            branch=next((x for x in domain['branches'] if x['id']==branch_id),None)
            if not branch:raise HTTPException(403,'指定分支不属于当前账号可见的领域')
            if not branch['selectable']:
                raise HTTPException(422,'该分支为只读或非草稿 development 分支，不能创建可写本体工作空间')
            # The entire remote read and validation MUST precede DB creation.
            try:catalog=canonical_catalog(client.catalog(branch_id))
            except OpenMindError as exc:
                raise HTTPException(502,'OpenMind 分支目录读取或校验失败，未创建工作空间：'+str(exc)) from exc
        finally:client.close()
        names=set()
        for e in catalog['entities']:
            key=e['name'].casefold()
            if key in names:raise HTTPException(409,'OpenMind 分支存在同名实体，无法可靠映射到本地工作空间，请先解决歧义')
            names.add(key)
        # Entity identity is (project_id, OpenMind UUID). The same remote
        # object can be reused independently across multiple workspaces.
        fingerprint=baseline_hash(catalog)
        counts={k:len(catalog[k]) for k in ('entities','attributes','relations','actions','capabilities')}
        with store.tx() as c:
            created=now()
            c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',(pid,body.name.strip(),created,created))
            c.execute('INSERT INTO openmind_bindings VALUES(?,?,?,?)',(pid,domain_id,branch_id,created))
            for ent in catalog['entities']:
                c.execute('''INSERT INTO entities (id,project_id,name,kind,description,class_id,evidence,origin)
                             VALUES (?,?,?,?,?,?,?,?)''',
                          (ent['id'],pid,ent['name'],'class',str(ent.get('description',''))[:1000],None,'[]','openmind'))
            c.execute('''INSERT INTO openmind_baselines
                (project_id,domain_id,branch_id,endpoint_hash,catalog_json,fingerprint,entity_count,attribute_count,
                 relation_count,action_count,capability_count,refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (pid,domain_id,branch_id,openmind_endpoint_hash(),j(catalog),fingerprint,
                 counts['entities'],counts['attributes'],counts['relations'],counts['actions'],counts['capabilities'],created))
            store.revision(c,pid)
            store.record_action(c,pid,'TOPIC',f'用户创建工作空间并选择 OpenMind 分支 {branch_id}',
                f'已读取分支基线：实体 {counts["entities"]}、属性 {counts["attributes"]}、关系 {counts["relations"]}。将以此为准逐步增量构建。')
            store.audit(c,pid,actor,'project_created',{'name':body.name,'mode':'openmind',
                        'domain_id':domain_id,'branch_id':branch_id,'fingerprint':fingerprint})
        return project_view(pid)

    @app.get('/api/projects',dependencies=[Depends(authorize)])
    def list_projects():
        return store.many('SELECT id,name,stage,revision,published_version,updated_at FROM projects ORDER BY updated_at DESC')

    @app.get('/api/projects/{pid}',dependencies=[Depends(authorize)])
    def get_view(pid:str):
        return project_view(pid)

    @app.delete('/api/projects/{pid}',dependencies=[Depends(authorize)])
    def delete_project(pid:str, body:ProjectDeleteIn, request:Request):
        """Permanently delete LOCAL project data; never mutate OpenMind.

        Guard by name + revision, and reject in-flight jobs to avoid racing an
        active model Worker. All DB deletes are committed atomically.
        """
        actor=authorize(request)
        files=[]
        with store.tx() as c:
            if store.is_postgres:
                p=c.execute('SELECT * FROM projects WHERE id=? FOR UPDATE',(pid,)).fetchone()
            else:
                c.execute('BEGIN IMMEDIATE')
                p=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
            if not p:
                raise HTTPException(404,'工作空间不存在或已删除')
            if body.confirm_name != p['name']:
                raise HTTPException(422,'工作空间名称不匹配；请完整输入名称以确认删除')
            if body.expected_revision != p['revision']:
                raise HTTPException(409,'工作空间已发生变化，请刷新后重新确认删除')
            if store.is_postgres:
                # Fence queued jobs before worker claim(SKIP LOCKED). Queued
                # jobs have not begun model work and may be deleted safely.
                c.execute("SELECT id FROM workflow_jobs WHERE project_id=? AND state='queued' FOR UPDATE",(pid,)).fetchall()
            active=c.execute("""SELECT COUNT(*) AS n FROM workflow_jobs
                WHERE project_id=? AND state='running'""",(pid,)).fetchone()['n']
            if active:
                raise HTTPException(409,f'工作空间仍有 {active} 个运行中的任务；请先让任务完成或停止 Worker，再删除，避免任务执行中数据丢失')
            from .task_leases import timestamp
            sync_lock=c.execute('SELECT lease_until FROM sync_locks WHERE project_id=?'+(' FOR UPDATE' if store.is_postgres else ''),(pid,)).fetchone()
            if sync_lock and sync_lock['lease_until']>timestamp():
                raise HTTPException(409,'OpenMind 同步或对账仍在执行，请待其结束再删除工作空间')
            rows=c.execute('SELECT id,path FROM documents WHERE project_id=?',(pid,)).fetchall()
            files=[(r['id'],r['path']) for r in rows]
            counts={}
            # Child tables first; do not rely on implicit DB cascades.
            for table in (
                'gap_decisions','openmind_operations','sync_locks','document_segments','relation_labels','attributes','relations','entities',
                'extraction_gaps','extraction_batch_audit','proposal_progress',
                'proposal_origins','modeling_decisions','modeling_contexts','proposals','summary_jobs','job_progress_events','workflow_jobs','messages',
                'releases','audit','openmind_sync_runs','openmind_mappings',
                'openmind_codes','openmind_baselines','openmind_bindings','documents',
            ):
                counts[table]=c.execute(f'SELECT COUNT(*) AS n FROM {table} WHERE project_id=?',(pid,)).fetchone()['n']
                c.execute(f'DELETE FROM {table} WHERE project_id=?',(pid,))
            c.execute("""INSERT INTO project_deletions(project_id,project_name,actor,deleted_at,counts_json)
                VALUES(?,?,?,?,?)""",(pid,p['name'],actor,now(),j(counts)))
            c.execute('DELETE FROM projects WHERE id=?',(pid,))
        # File deletion after commit: cannot roll back DB, so failures are
        # reported and logged without turning a successful delete into 500.
        skipped=[]
        uploads=store.uploads.resolve()
        for docid,raw in files:
            try:
                path=Path(raw)
                if (path.is_symlink() or path.resolve().parent!=uploads
                    or not path.name.startswith(docid+'.')):
                    skipped.append(path.name)
                    continue
                path.unlink(missing_ok=True)
            except OSError:
                skipped.append(Path(raw).name)
        if skipped:
            logging.getLogger('ontoflow').warning('Workspace %s deleted but %s upload files need manual cleanup',pid,len(skipped))
        return {'deleted':True,'project_id':pid,'remote_unchanged':True,
                'file_cleanup_pending':len(skipped),'deleted_counts':counts}

    @app.post('/api/projects/{pid}/documents',dependencies=[Depends(authorize)])
    async def upload(pid:str,request:Request,file:UploadFile=File(...)):
        get_project(pid)
        filename=Path(file.filename or 'unknown').name.replace('\x00','')[:180]
        ext=Path(filename).suffix.lower()
        try:require_reader(ext)
        except ValueError as exc:
            await file.close()
            raise HTTPException(415,str(exc)) from exc
        content=await file.read(MAX_UPLOAD+1)
        await file.close()
        if not content:raise HTTPException(400,'文件为空。')
        if len(content)>MAX_UPLOAD:raise HTTPException(413,f'文件超过 {MAX_UPLOAD//1024//1024} MB 上限。')
        sha=hashlib.sha256(content).hexdigest()
        exists=store.one('SELECT id FROM documents WHERE project_id=? AND sha256=?',(pid,sha))
        if exists:raise HTTPException(409,'该项目已存在内容完全相同的文件。')
        docid=uid();target=store.uploads/(docid+ext)
        target.write_bytes(content)
        try:
            if (store.is_postgres and os.getenv('ASYNC_PARSE','1')=='1') or os.getenv('ASYNC_PARSE')=='1':
                jid=uid()
                with store.tx() as c:
                    c.execute('INSERT INTO documents(id,project_id,filename,sha256,bytes,path,content,chunks,warnings,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (docid,pid,filename,sha,len(content),str(target),'','[]','[]',now()))
                    c.execute("UPDATE projects SET stage='TOPIC',scope_confirmed=0,candidate_summary='',candidate_scope='',updated_at=? WHERE id=?",(now(),pid))
                    c.execute("INSERT INTO workflow_jobs (id,project_id,job_kind,stage,state,phase,completed_units,total_units,detail,error,source_fingerprint,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (jid,pid,'parse',docid,'queued','queued',0,1,'等待文档解析 Worker','',docid,'{}',now(),now()))
                    store.record_action(c,pid,'UPLOAD',f'用户上传文档「{filename}」。','已入队等待解析，完成后再生成概要。')
                    store.audit(c,pid,authorize(request),'document_queued',{'doc_id':docid,'job_id':jid})
                return {'document':public_doc(store.one('SELECT * FROM documents WHERE id=?',(docid,))),
                        'chunks':0,'queued':True,'job_id':jid}
        except Exception as exc:
            target.unlink(missing_ok=True)
            if isinstance(exc,HTTPException):raise
            logging.getLogger('ontoflow').exception('Document enqueue failed; source file removed')
            raise HTTPException(503,'文档未入队，上传文件已清理；请检查数据库或重试，不会留下半条文档。') from exc
        try:
            parsed=extract(target)
        except ExtractionError as e:
            target.unlink(missing_ok=True)
            raise HTTPException(422,str(e))
        with store.tx() as c:
            c.execute('INSERT INTO documents(id,project_id,filename,sha256,bytes,path,content,chunks,warnings,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (docid,pid,filename,sha,len(content),str(target),parsed['content'],j(parsed['chunks']),j(parsed['warnings']),now()))
            c.execute("UPDATE projects SET stage='TOPIC',scope_confirmed=0,candidate_summary='',candidate_scope='',updated_at=? WHERE id=?",(now(),pid))
            store.record_action(c,pid,'UPLOAD',f'用户上传文档「{filename}」。',
                                f'已解析文件「{filename}」，生成 {len(parsed["chunks"])} 个可追溯片段，等待生成概要并确认主题。')
            store.audit(c,pid,authorize(request),'document_uploaded',{'doc_id':docid,'filename':filename,'sha256':sha})
        incremental.persist_segments(store,store.one('SELECT * FROM documents WHERE id=?',(docid,)))
        return {'document':public_doc(store.one('SELECT * FROM documents WHERE id=?',(docid,))),'chunks':len(parsed['chunks'])}

    @app.post('/api/projects/{pid}/documents/reparse',dependencies=[Depends(authorize)])
    def reparse_documents(pid:str,request:Request):
        """Re-extract from preserved originals; repairs previously truncated Office cells.

        Never quietly replace historical versions or change approved ontology facts.
        """
        get_project(pid)
        original = docs(pid)
        if not original:
            raise HTTPException(409,'当前项目没有可重新解析的文档。')
        parsed_items=[]
        for d in original:
            source=Path(d['path'])
            if not source.is_file():
                raise HTTPException(422,f'原始文件已丢失，无法重新解析：{d["filename"]}。请恢复上传文件备份。')
            try:
                parsed=extract(source)
            except ExtractionError as exc:
                raise HTTPException(422,f'重新解析 {d["filename"]} 失败：{exc}') from exc
            parsed_items.append((d,parsed))
        with store.tx() as conn:
            for old,parsed in parsed_items:
                conn.execute('UPDATE documents SET content=?,chunks=?,warnings=?,content_revision=content_revision+1 WHERE id=? AND project_id=?',
                             (parsed['content'],j(parsed['chunks']),j(parsed['warnings']),old['id'],pid))
            conn.execute("UPDATE projects SET stage='TOPIC',scope_confirmed=0,candidate_summary='',candidate_scope='',updated_at=? WHERE id=?",
                         (now(),pid))
            store.record_action(conn,pid,'TOPIC',f'用户要求重新解析 {len(parsed_items)} 份原始文件。',
                                '已从保留的原件重新提取文本，原有本体定义保持不变，需重新确认主题。')
            store.audit(conn,pid,authorize(request),'documents_reparsed',{
                'count':len(parsed_items),'documents':[{
                    'id':old['id'],'filename':old['filename'],'before_chars':len(old['content']),
                    'after_chars':len(parsed['content']),'chunks':len(parsed['chunks'])
                } for old,parsed in parsed_items]})
        for old,_ in parsed_items:
            incremental.persist_segments(store,store.one('SELECT * FROM documents WHERE id=?',(old['id'],)))
        return {'reparsed':len(parsed_items),'documents':[{
            'id':old['id'],'filename':old['filename'],'before_chars':len(old['content']),
            'after_chars':len(parsed['content']),'chunks':len(parsed['chunks'])
        } for old,parsed in parsed_items],
                'notice':'已重新解析原始文件。请重新生成概要并确认主题；原有本体与历史发布版本未清空。'}

    @app.get('/api/projects/{pid}/documents/{docid}/chunks',dependencies=[Depends(authorize)])
    def chunks(pid:str,docid:str):
        get_project(pid)
        d=store.one('SELECT * FROM documents WHERE id=? AND project_id=?',(docid,pid))
        if not d:raise HTTPException(404,'文档不存在。')
        return {'document':public_doc(d),'chunks':json.loads(d['chunks'])}

    @app.post('/api/projects/{pid}/analyze',dependencies=[Depends(authorize)])
    def analyze(pid:str,request:Request):
        if store.is_postgres:
            raise HTTPException(409,'生产环境请使用 /api/projects/{pid}/jobs/start?kind=summary 异步任务')
        get_project(pid)
        documents=docs(pid)
        if not documents:raise HTTPException(409,'请先上传文档。')
        if any(not d['content'].strip() for d in documents):raise HTTPException(409,'文档尚未解析完成，请查看解析任务。')
        baseline=require_baseline(pid)
        try:
            with brain.using_model(configured_model(pid)):
                result=brain.analyze(documents,baseline=baseline)
        except (ValueError,RuntimeError) as e:raise HTTPException(502,str(e))
        with store.tx() as c:
            c.execute("UPDATE projects SET candidate_summary=?,candidate_scope=?,stage='TOPIC',scope_confirmed=0,updated_at=? WHERE id=?",
                      (result['summary'],result['scope'],now(),pid))
            store.audit(c,pid,authorize(request),'analyze',{'mode':model_settings.public()['mode']})
        return result

    @app.put('/api/projects/{pid}/topic',dependencies=[Depends(authorize)])
    def confirm_topic(pid:str,body:TopicIn,request:Request):
        p=get_project(pid)
        require_baseline(pid)
        if not docs(pid):raise HTTPException(409,'请先上传资料。')
        with store.tx() as c:
            c.execute("UPDATE projects SET scope=?,summary=?,scope_confirmed=1,stage='ENTITY',updated_at=? WHERE id=?",
                      (body.scope.strip(),body.summary.strip(),now(),pid))
            store.revision(c,pid)
            store.record_action(c,pid,'TOPIC',f'用户确认本体主题与边界：{body.scope.strip()}',
                                '主题已经保存并确认，现进入实体构建。后续候选仍需逐项人工审核。')
            store.audit(c,pid,authorize(request),'topic_confirmed',{'scope':body.scope})
        return project_view(pid)

    @app.post('/api/projects/{pid}/stage',dependencies=[Depends(authorize)])
    def change_stage(pid:str,body:StageIn,request:Request):
        p=get_project(pid); target=body.target
        require_baseline(pid)
        if target not in STAGES:raise HTTPException(400,'非法阶段。')
        if not p['scope_confirmed'] and target not in ('UPLOAD','TOPIC'):
            raise HTTPException(409,'请先确认建模主题。')
        if target in ('UPLOAD','TOPIC'):
            raise HTTPException(409,'请通过上传或重新分析文档回到主题确认。')
        if STAGES.index(target)>STAGES.index(p['stage'])+1:
            raise HTTPException(409,'阶段必须逐步推进，不可跳过。')
        with store.tx() as c:
            c.execute('UPDATE projects SET stage=?,updated_at=? WHERE id=?',(target,now(),pid))
            store.record_action(c,pid,target,f'用户将构建阶段由 {p["stage"]} 切换到 {target}。',
                                f'已进入 {target} 阶段，继续保持之前已确认的本体定义。')
            store.audit(c,pid,authorize(request),'stage_changed',{'from':p['stage'],'to':target})
        return project_view(pid)

    @app.post('/api/projects/{pid}/propose',dependencies=[Depends(authorize)])
    def propose(pid:str,request:Request):
        if store.is_postgres:
            raise HTTPException(409,'生产环境请使用 /api/projects/{pid}/jobs/start?kind=propose 异步任务')
        p=get_project(pid)
        if not p['scope_confirmed'] or p['stage'] not in KIND_STAGE.values():
            raise HTTPException(409,'请先确认主题并进入实体、属性或关系阶段。')
        config=configured_model(pid)
        baseline=require_baseline(pid)
        source_docs=docs(pid)
        # Progress is scoped to the *actual* document content, topic, stage and
        # selected model. Reparsing or re-uploading changes the fingerprint.
        signature=hashlib.sha256(json.dumps({
            'stage':p['stage'],'scope':p['scope'],
            'model':config.get('model',''),'base_url':config.get('base_url',''),
            'documents':[(d['id'],d['chunks']) for d in source_docs],
            'openmind_baseline':((baseline_row(pid) or {}).get('fingerprint','') if current_baseline(pid) is not None else ''),
            'approved_entities':[(x['id'],x['name']) for x in store.snapshot(pid)['entities']]
                 if p['stage'] in ('PROPERTY','RELATION') else []
        },ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
        last=store.one('SELECT * FROM proposal_progress WHERE project_id=? AND stage=?',
                       (pid,p['stage']))
        next_batch=(last['next_batch'] if last and last['source_fingerprint']==signature else 0)
        try:
            with brain.using_model(config):
                progress=brain.proposal_batches(p['stage'],source_docs,store.snapshot(pid),p['scope'],
                                                 start_batch=next_batch,max_batches=3,baseline=baseline)
        except (ValueError,RuntimeError) as e:
            raise HTTPException(502,str(e))
        result=add_proposals(pid,p['stage'],progress['items'],
                             'model' if config['base_url'] and config['model'] else 'rules',authorize(request))
        with store.tx() as conn:
            row=conn.execute('SELECT stage,scope_confirmed FROM projects WHERE id=?',(pid,)).fetchone()
            if not row or row['stage']!=p['stage'] or not row['scope_confirmed']:
                raise HTTPException(409,'建模阶段已发生变化，请刷新后重试。')
            conn.execute('''INSERT INTO proposal_progress
                (project_id,stage,source_fingerprint,next_batch,total_batches,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(project_id,stage) DO UPDATE SET
                  source_fingerprint=excluded.source_fingerprint,
                  next_batch=excluded.next_batch,
                  total_batches=excluded.total_batches,
                  updated_at=excluded.updated_at''',
                (pid,p['stage'],signature,progress['next_batch'],progress['total'],now()))
        return {**result,'progress':{
            'processed_batches':progress['next_batch'],
            'total_batches':progress['total'],
            'complete':progress['complete'],
            'next_batch':progress['next_batch'],
            'batch_count_this_call':progress['processed']}}

    @app.post('/api/projects/{pid}/proposals',dependencies=[Depends(authorize)])
    def create_manual_proposal(pid:str,body:ProposalIn,request:Request):
        p=get_project(pid)
        if not p['scope_confirmed']:raise HTTPException(409,'请先确认主题。')
        stage=KIND_STAGE.get(body.kind)
        if stage != p['stage']:raise HTTPException(409,'请先切换到对应构建阶段，才能新建或修改该类候选。')
        if not stage:raise HTTPException(400,'候选类型无效。')
        result=add_proposals(pid,stage,[body.model_dump()],'human',authorize(request))
        if not result['created'] and not result['reused']:
            raise HTTPException(422,'无法创建候选：'+'; '.join(result['rejected']))
        return result

    def apply_proposal_decision(c,pid,q,review_action,review_note='',modified_payload=None):
        """Apply a single human decision inside caller-owned SQLite transaction.

        Shared by single and batch paths so references, inheritance, and other
        existing validation cannot diverge between approval modalities.
        """
        c.execute('UPDATE projects SET proposal_generation=proposal_generation+1 WHERE id=?',(pid,))
        payload=json.loads(q['payload']); evidence=q['evidence']
        if review_action=='accept':
            policy=modeling.runtime_in_tx(c,pid)
            origin=c.execute('SELECT context_fingerprint FROM proposal_origins WHERE proposal_id=? AND project_id=?',(q['id'],pid)).fetchone()
            if ((origin and origin['context_fingerprint'] and origin['context_fingerprint']!=policy['fingerprint'])
                or ((not origin or not origin['context_fingerprint']) and policy.get('decision_version',0)>0)):
                raise HTTPException(409,'此候选基于旧的建模意图或分支。请重新生成，或先按当前意图人工复核；不会直接同意旧建议。')
            if modified_payload is not None:
                modeling.save_proposal_origin(c,pid,q['id'],{'kind':'user_decision','method':'edited_on_review',
                    'rationale':review_note.strip() or '用户在审批时编辑了候选，本次修改作为人工建模决定'},policy['fingerprint'])
            snapshot=store.snapshot(pid,c)
            proposed=modified_payload if modified_payload is not None else payload
            # Preserve the operation type and target identity on a modified proposal;
            # a user may change fields but not silently turn an add into a delete.
            if modified_payload is not None:
                proposed=dict(proposed)
                for locked in ('operation','record_id'):
                    if locked in payload:proposed[locked]=payload[locked]
                if q['kind'] != 'relation' and payload.get('operation') in ('update','delete'):
                    proposed['target_id']=payload['target_id']
            proposed=prepare_payload(q['kind'],proposed,json.loads(evidence),snapshot)
            try:clean=validate_payload(q['kind'],proposed,snapshot)
            except InvalidOntology as e:raise HTTPException(422,str(e))
            operation=clean.get('operation','add')
            if operation!='delete':
                problems=naming_errors(q['kind'],clean)
                if problems:raise HTTPException(422,'；'.join(problems))
                # Existing mapped updates are checked against their stable code
                # below and against the complete remote object by the write planner.
                local_conflict=assignment_error(q['kind'],clean,json.loads(evidence),snapshot)
                if local_conflict:raise HTTPException(409,local_conflict)
                identity=clean.get('record_id') if q['kind']=='relation' else clean.get('target_id')
                if operation=='update':
                    mapped=c.execute('SELECT code FROM openmind_mappings WHERE project_id=? AND kind=? AND local_id=?',
                                     (pid,q['kind'],identity)).fetchone()
                    if mapped and clean['code']!=mapped['code']:
                        raise HTTPException(409,'已同步对象的英文 CODE 不能通过重命名修改；中文名称可经审核变更。')
            object_id=None
            if operation=='add':
                object_id=uid()
                if q['kind']=='entity':
                    c.execute('INSERT INTO entities (id,project_id,name,kind,description,class_id,evidence) VALUES(?,?,?,?,?,?,?)',
                              (object_id,pid,clean['name'],clean['kind'],clean['description'],clean['class_id'],evidence))
                elif q['kind']=='attribute':
                    c.execute('INSERT INTO attributes VALUES(?,?,?,?,?,?,?,?,?)',
                              (object_id,pid,clean['owner_id'],clean['name'],clean['datatype'],int(clean['required']),int(clean['multivalued']),clean['description'],evidence))
                else:
                    c.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?)',
                              (object_id,pid,clean['source_id'],clean['target_id'],clean['predicate'],clean['description'],evidence))
            elif operation=='update':
                target_id=clean.get('record_id',clean.get('target_id'))
                object_id=target_id
                if q['kind']=='entity':
                    c.execute('UPDATE entities SET name=?,description=?,class_id=?,evidence=? WHERE id=? AND project_id=?',
                              (clean['name'],clean['description'],clean['class_id'],evidence,target_id,pid))
                elif q['kind']=='attribute':
                    c.execute('UPDATE attributes SET owner_id=?,name=?,datatype=?,required=?,multivalued=?,description=?,evidence=? WHERE id=? AND project_id=?',
                              (clean['owner_id'],clean['name'],clean['datatype'],int(clean['required']),int(clean['multivalued']),clean['description'],evidence,target_id,pid))
                else:
                    c.execute('UPDATE relations SET source_id=?,target_id=?,predicate=?,description=?,evidence=? WHERE id=? AND project_id=?',
                              (clean['source_id'],clean['target_id'],clean['predicate'],clean['description'],evidence,target_id,pid))
            elif operation=='delete':
                target_id=clean.get('record_id',clean.get('target_id'))
                if q['kind']=='entity':
                    has_refs=(c.execute('SELECT id FROM attributes WHERE project_id=? AND owner_id=? LIMIT 1',(pid,target_id)).fetchone()
                             or c.execute('SELECT id FROM relations WHERE project_id=? AND (source_id=? OR target_id=?) LIMIT 1',(pid,target_id,target_id)).fetchone()
                             or c.execute('SELECT id FROM entities WHERE project_id=? AND class_id=? LIMIT 1',(pid,target_id)).fetchone())
                    if has_refs:raise HTTPException(409,'该实体仍被属性、关系或实例引用，请先删除引用对象。')
                    c.execute('DELETE FROM entities WHERE id=? AND project_id=?',(target_id,pid))
                elif q['kind']=='attribute':
                    c.execute('DELETE FROM attributes WHERE id=? AND project_id=?',(target_id,pid))
                else:
                    c.execute('DELETE FROM relation_labels WHERE relation_id=? AND project_id=?',(target_id,pid))
                    c.execute('DELETE FROM relations WHERE id=? AND project_id=?',(target_id,pid))
                c.execute('DELETE FROM openmind_codes WHERE project_id=? AND kind=? AND local_id=?',(pid,q['kind'],target_id))
            if object_id is not None:save_metadata(c,pid,q['kind'],object_id,clean)
            c.execute("UPDATE proposals SET status='accepted',payload=?,review_note=?,decided_at=? WHERE id=?",
                      (j(clean),review_note,now(),q['id']))
        else:
            c.execute("UPDATE proposals SET status='rejected',review_note=?,decided_at=? WHERE id=?",(review_note,now(),q['id']))
        return clean if review_action=='accept' else payload

    @app.post('/api/projects/{pid}/proposals/{proposal_id}/decision',dependencies=[Depends(authorize)])
    def decision(pid:str,proposal_id:str,body:DecisionIn,request:Request):
        get_project(pid)
        if body.action not in ('accept','reject'):raise HTTPException(400,'action 必须是 accept 或 reject。')
        actor=authorize(request)
        with store.tx() as c:
            c.execute('SELECT id FROM projects WHERE id=? FOR UPDATE',(pid,)) if store.is_postgres else c.execute('BEGIN IMMEDIATE')
            q=c.execute('SELECT * FROM proposals WHERE id=? AND project_id=?',(proposal_id,pid)).fetchone()
            if not q:raise HTTPException(404,'候选不存在。')
            if q['status']!='pending':raise HTTPException(409,'该候选已被审核。')
            if body.action=='accept' and not c.execute('SELECT scope_confirmed FROM projects WHERE id=?',(pid,)).fetchone()['scope_confirmed']:
                raise HTTPException(409,'新资料尚未确认建模主题，不能审批写入正式草稿。')
            current=c.execute('SELECT revision FROM projects WHERE id=?',(pid,)).fetchone()['revision']
            if body.expected_revision is not None and current!=body.expected_revision:
                raise HTTPException(409,'当前本体已发生变更，请刷新后重新确认。')
            payload=apply_proposal_decision(c,pid,q,body.action,body.note,body.payload)
            if body.action=='accept':store.revision(c,pid)
            kind_name={'entity':'实体','attribute':'属性','relation':'关系'}[q['kind']]
            label=(payload.get('name') or payload.get('predicate') or proposal_id)
            act='确认' if body.action=='accept' else '拒绝'
            statement=f'用户{act}{kind_name}候选「{label}」' + (f'；说明：{body.note}' if body.note else '')
            acknowledgement=(f'候选「{label}」已通过人工审核并写入本体草稿。' if body.action=='accept'
                             else f'候选「{label}」已拒绝，不会写入本体草稿。')
            store.record_action(c,pid,q['stage'],statement,acknowledgement)
            store.audit(c,pid,actor,'proposal_'+body.action,{'proposal_id':proposal_id,'kind':q['kind'],'note':body.note})
        return project_view(pid)

    @app.get('/api/projects/{pid}/proposals/review-all/preview',dependencies=[Depends(authorize)])
    def review_all_preview(pid:str,kind:str):
        if kind not in KIND_STAGE:raise HTTPException(422,'候选类型无效')
        from .review_all import make_snapshot
        with store.tx() as c:
            if store.is_postgres:c.execute('SELECT id FROM projects WHERE id=? FOR UPDATE',(pid,))
            else:c.execute('BEGIN IMMEDIATE')
            snapshot=make_snapshot(c,pid,kind,KIND_STAGE[kind])
        return {k:v for k,v in snapshot.items() if k!='items'} | {'examples':[x['label'] for x in snapshot['items'][:5]]}

    @app.post('/api/projects/{pid}/proposals/review-all',status_code=202,dependencies=[Depends(authorize)])
    def start_review_all(pid:str,body:ReviewAllIn,request:Request):
        if body.kind not in KIND_STAGE or body.confirmation!='CONFIRM_ACCEPT_ALL_PENDING':
            raise HTTPException(422,'需明确确认当前阶段全部待审候选')
        from .review_all import make_snapshot
        stage=KIND_STAGE[body.kind];actor=authorize(request);jid=uid()
        with store.tx() as c:
            if store.is_postgres:c.execute('SELECT id FROM projects WHERE id=? FOR UPDATE',(pid,))
            else:c.execute('BEGIN IMMEDIATE')
            existing=c.execute("SELECT * FROM workflow_jobs WHERE project_id=? AND job_kind='review_all' AND stage=? AND state IN ('queued','running')",(pid,stage)).fetchone()
            if existing:return {'job':dict(existing),'already_running':True}
            snapshot=make_snapshot(c,pid,body.kind,stage)
            if snapshot['revision']!=body.expected_revision or snapshot['fingerprint']!=body.fingerprint:
                raise HTTPException(409,'待审清单或本体版本已改变，请重新查看全部同意确认框；未执行任何审批')
            if not snapshot['total']:raise HTTPException(409,'当前阶段没有待审候选')
            report={**snapshot,'actor':actor,'note':body.note,'cursor':0,
                    'succeeded':0,'failed':0,'failures':[],'unprocessed':snapshot['total']}
            c.execute("""INSERT INTO workflow_jobs (id,project_id,job_kind,stage,state,phase,completed_units,total_units,
                         detail,error,source_fingerprint,result,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid,pid,'review_all',stage,'queued','queued',0,snapshot['total'],
                 f'全部同意已确认 {snapshot["total"]} 条（跨页），等待处理','',body.fingerprint,j(report),now(),now()))
            store.record_action(c,pid,stage,f'用户确认跨页全部同意 {snapshot["total"]} 条{body.kind}候选。',
                                '已固定本次清单；逐条审核，单条失败继续，后续新增候选不自动纳入。只修改本地草稿。')
            store.audit(c,pid,actor,'review_all_confirmed',{'job_id':jid,'count':snapshot['total'],'fingerprint':body.fingerprint})
        if not store.is_postgres:
            threading.Thread(target=run_job,args=(pid,jid,'review_all',stage,body.fingerprint,{},actor),daemon=True).start()
        return {'job':store.one('SELECT * FROM workflow_jobs WHERE id=?',(jid,)),'already_running':False}

    @app.post('/api/projects/{pid}/proposals/batch-decision',dependencies=[Depends(authorize)])
    def batch_decision(pid:str,body:BulkDecisionIn,request:Request):
        """Per-item commit: any invalid candidate remains pending; others continue.

        Failures are returned with stable IDs; no silent skips, no batch rollback.
        Each accepted item increments the project revision independently.
        """
        get_project(pid)
        if body.action not in ('accept','reject') or body.kind not in KIND_STAGE:
            raise HTTPException(422,'无效批量审核类型或操作')
        if len(set(body.proposal_ids))!=len(body.proposal_ids):
            raise HTTPException(422,'候选 ID 重复')
        actor=authorize(request)
        revision=body.expected_revision
        results=[]
        for proposal_id in body.proposal_ids:
            try:
                with store.tx() as c:
                    if store.is_postgres:
                        c.execute('SELECT revision FROM projects WHERE id=? FOR UPDATE',(pid,))
                    else:
                        c.execute('BEGIN IMMEDIATE')
                    p=c.execute('SELECT revision,scope_confirmed FROM projects WHERE id=?',(pid,)).fetchone()
                    if not p or p['revision']!=revision:
                        raise HTTPException(409,'本体修订已变化，请刷新当前候选')
                    if body.action=='accept' and not p['scope_confirmed']:
                        raise HTTPException(409,'主题未确认，不能接受候选')
                    q=c.execute('SELECT * FROM proposals WHERE id=? AND project_id=?',(proposal_id,pid)).fetchone()
                    if q is None:raise HTTPException(404,'候选不存在')
                    if q['kind']!=body.kind:raise HTTPException(422,'候选类型不匹配')
                    if q['status']!='pending':raise HTTPException(409,'该候选已被审核')
                    payload=apply_proposal_decision(c,pid,q,body.action,body.note)
                    if body.action=='accept':
                        store.revision(c,pid)
                        revision+=1
                    label=str(payload.get('name') or payload.get('predicate') or proposal_id)
                    verb='确认' if body.action=='accept' else '拒绝'
                    store.record_action(c,pid,q['stage'],f'用户批量{verb}{body.kind}候选「{label}」',
                        f'候选「{label}」审核成功；其他候选的操作失败不会撤销本次结果。')
                    store.audit(c,pid,actor,'proposal_bulk_item_'+body.action,
                                {'proposal_id':proposal_id,'note':body.note})
                results.append({'id':proposal_id,'status':'accepted' if body.action=='accept' else 'rejected'})
            except HTTPException as exc:
                results.append({'id':proposal_id,'status':'failed','code':exc.status_code,'reason':str(exc.detail)})
            except Exception as exc:
                # DB constraint failure must not prevent the remaining selected rows.
                # Never include database connection strings or secret parameters.
                from .ontology import InvalidOntology
                if isinstance(exc,InvalidOntology):
                    reason=str(exc)[:350]
                else:
                    reason='数据库约束或并发写入失败；本项保持原状态，详情请查看服务端日志'
                results.append({'id':proposal_id,'status':'failed','code':409,'reason':reason})
        ok=[r for r in results if r['status']!='failed']
        failed=[r for r in results if r['status']=='failed']
        return {'action':body.action,'kind':body.kind,'requested':len(results),
                'processed':len(ok),'succeeded':len(ok),'failed':len(failed),
                'results':results,'revision':revision}

    @app.post('/api/projects/{pid}/chat',dependencies=[Depends(authorize)])
    def conversation(pid:str,body:ChatIn,request:Request):
        p=get_project(pid)
        if p['stage']=='UPLOAD':raise HTTPException(409,'请先上传文档。')
        history=store.many('SELECT role,content,source FROM messages WHERE project_id=? ORDER BY rowid DESC LIMIT 24',(pid,))
        with store.tx() as c:
            c.execute('INSERT INTO messages (id,project_id,stage,role,content,created_at,source) VALUES(?,?,?,?,?,?,?)',(uid(),pid,p['stage'],'user',body.message,now(),'chat'))
        config=configured_model(pid)
        gaps=[{'reason':r['reason'],'kind':r['kind'],
               'payload':json.loads(r['payload']),'batch':r['batch_number']}
              for r in store.many("SELECT reason,kind,payload,batch_number FROM extraction_gaps WHERE project_id=? AND stage=? AND status='open' ORDER BY rowid DESC LIMIT 12",(pid,p['stage']))]
        try:
            with brain.using_model(config):
                ans=brain.chat(body.message,p['stage'],p['scope'],store.snapshot(pid),docs(pid),list(reversed(history)),
                               baseline=require_baseline(pid),clarifications=gaps)
        except (ValueError,RuntimeError) as e:raise HTTPException(502,str(e))
        with store.tx() as c:
            c.execute('INSERT INTO messages (id,project_id,stage,role,content,created_at,source) VALUES(?,?,?,?,?,?,?)',(uid(),pid,p['stage'],'assistant',ans['reply'],now(),'chat'))
            store.audit(c,pid,authorize(request),'discussion',{'stage':p['stage'],'model':bool(config['base_url'] and config['model'])})
        added={'created':[],'rejected':[]}
        if p['scope_confirmed'] and p['stage'] in KIND_STAGE.values() and ans['proposals']:
            added=add_proposals(pid,p['stage'],ans['proposals'],'model',authorize(request),
                context={'policy_fingerprint':config['interpretation']['fingerprint']})
        return {'reply':ans['reply'],**added}

    @app.get('/api/projects/{pid}/diff',dependencies=[Depends(authorize)])
    def diff(pid:str):
        p=get_project(pid);latest=store.one('SELECT * FROM releases WHERE project_id=? ORDER BY version DESC LIMIT 1',(pid,))
        previous=json.loads(latest['snapshot']) if latest else {'entities':[],'attributes':[],'relations':[]}
        current=store.snapshot(pid)
        changes={}
        for field in ('entities','attributes','relations'):
            old={x['id']:x for x in previous[field]};new={x['id']:x for x in current[field]}
            changes[field]={'added':[v for k,v in new.items() if k not in old],
                            'removed':[v for k,v in old.items() if k not in new],
                            'modified':[v for k,v in new.items() if k in old and old[k]!=v]}
        return {'base_version':latest['version'] if latest else 0,'revision':p['revision'],'changes':changes,'validation':check_ontology(current)}

    @app.post('/api/projects/{pid}/publish',dependencies=[Depends(authorize)])
    def publish(pid:str,body:PublishIn,request:Request):
        p=get_project(pid)
        require_baseline(pid)
        if p['stage']!='REVIEW' or not p['scope_confirmed']:raise HTTPException(409,'请先完成主题确认并进入审核发布阶段。')
        with store.tx() as c:
            snap=store.snapshot(pid,c);valid=check_ontology(snap)
            issues=[]
            for k,field in (('entity','entities'),('attribute','attributes'),('relation','relations')):
                for obj in snap[field]:
                    if obj.get('origin')=='openmind':continue
                    for reason in naming_errors(k,obj):issues.append(f'{obj.get("name") or obj.get("predicate") or obj["id"]}：{reason}')
            if issues:raise HTTPException(422,issues)
            if not valid['valid']:raise HTTPException(422,valid['issues'])
            pending=c.execute("SELECT COUNT(*) n FROM proposals WHERE project_id=? AND status='pending'",(pid,)).fetchone()['n']
            last=c.execute('SELECT revision FROM releases WHERE project_id=? ORDER BY version DESC LIMIT 1',(pid,)).fetchone()
            if last and last['revision']==p['revision']:raise HTTPException(409,'本体与上次发布版本一致，无需重复发布。')
            version=p['published_version']+1
            c.execute('INSERT INTO releases VALUES(?,?,?,?,?,?,?)',(uid(),pid,version,p['revision'],j(snap),body.note,now()))
            c.execute('UPDATE projects SET published_version=?,updated_at=? WHERE id=?',(version,now(),pid))
            store.record_action(c,pid,'REVIEW',f'用户确认发布本体版本 v{version}；备注：{body.note}',
                                f'本地本体 v{version} 已发布。此操作不代表 OpenMind 已上线。')
            store.audit(c,pid,authorize(request),'published',{'version':version,'revision':p['revision'],'pending_excluded':pending})
        return {'published_version':version,'revision':p['revision'],'validation':valid}

    @app.post('/api/projects/{pid}/releases/{version}/restore',dependencies=[Depends(authorize)])
    def restore(pid:str,version:int,request:Request):
        get_project(pid)
        actor=authorize(request)
        with store.tx() as c:
            old=c.execute('SELECT snapshot FROM releases WHERE project_id=? AND version=?',(pid,version)).fetchone()
            if not old:raise HTTPException(404,'版本不存在。')
            snap=json.loads(old['snapshot'])
            # Keep releases and review audit immutable. A restore creates a new draft revision.
            c.execute('DELETE FROM relation_labels WHERE project_id=?',(pid,))
            c.execute('DELETE FROM relations WHERE project_id=?',(pid,))
            c.execute('DELETE FROM attributes WHERE project_id=?',(pid,))
            # Read-only OpenMind entity anchors survive local version restores.
            c.execute("DELETE FROM entities WHERE project_id=? AND origin='local'",(pid,))
            for e in snap['entities']:
                if e.get('origin')=='openmind':
                    if not c.execute("SELECT 1 FROM entities WHERE id=? AND project_id=? AND origin='openmind'",(e['id'],pid)).fetchone():
                        raise HTTPException(409,'历史版本引用的远端实体已不在当前分支基线中；请先刷新并解决冲突。')
                    continue
                c.execute('INSERT INTO entities (id,project_id,name,kind,description,class_id,evidence) VALUES(?,?,?,?,?,?,?)',
                          tuple(e[k] for k in ('id','project_id','name','kind','description','class_id','evidence')))
            for a in snap['attributes']:
                c.execute('INSERT INTO attributes VALUES(?,?,?,?,?,?,?,?,?)',tuple(a[k] for k in ('id','project_id','owner_id','name','datatype','required','multivalued','description','evidence')))
            for r in snap['relations']:
                c.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?)',tuple(r[k] for k in ('id','project_id','source_id','target_id','predicate','description','evidence')))
            for k,field in (('entity','entities'),('attribute','attributes'),('relation','relations')):
                for obj in snap[field]:
                    if obj.get('origin')!='openmind':save_metadata(c,pid,k,obj['id'],obj)
            c.execute("UPDATE proposals SET status='rejected',review_note='恢复旧版时关闭待审核候选',decided_at=? WHERE project_id=? AND status='pending'",(now(),pid))
            c.execute("UPDATE projects SET stage='REVIEW',scope_confirmed=1,summary=?,scope=?,updated_at=? WHERE id=?",
                      (snap['project']['summary'],snap['project']['scope'],now(),pid))
            store.revision(c,pid)
            store.record_action(c,pid,'REVIEW',f'用户恢复本体历史版本 v{version}。',
                                '已将历史版本还原为新的可审核草稿，正式发布版本保持不变。')
            store.audit(c,pid,actor,'restored',{'from_version':version})
        return project_view(pid)

    @app.get('/api/projects/{pid}/export',dependencies=[Depends(authorize)])
    def export(pid:str,format:str='ttl',version:int|None=None):
        get_project(pid)
        if version is not None:
            release=store.one('SELECT snapshot FROM releases WHERE project_id=? AND version=?',(pid,version))
            if not release:raise HTTPException(404,'发布版本不存在。')
            snap=json.loads(release['snapshot']); suffix=f'-v{version}'
        else:
            snap=store.snapshot(pid);suffix='-draft'
        if format=='json':
            content=json.dumps(snap,ensure_ascii=False,indent=2)
            media='application/json';extension='json'
        elif format in ('ttl','owl','jsonld'):
            content=serialize_ontology(snap,format)
            media={'ttl':'text/turtle','owl':'application/rdf+xml','jsonld':'application/ld+json'}[format]
            extension=format
        else:raise HTTPException(400,'仅支持 format=json、ttl、owl 或 jsonld。')
        return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="ontology-{pid}{suffix}.{extension}"'})

    @app.get('/api/projects/{pid}/audit',dependencies=[Depends(authorize)])
    def audits(pid:str):
        get_project(pid)
        return store.many('SELECT * FROM audit WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 200',(pid,))

    return app


app=create_app()
