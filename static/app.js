/* Self-hosted frontend. No CDN, framework, or remote assets required. */
'use strict';
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S={demoMode:false,projects:[],view:null,id:null,tab:'overview',token:sessionStorage.getItem('ontologyToken')||'',actor:'local-demo',busy:false,modelAdminToken:'',openmindAdminToken:'',jobPollTimer:null,batchSelections:{entity:new Set(),attribute:new Set(),relation:new Set()},pendingPage:{entity:1,attribute:1,relation:1},pendingData:{},pageSize:20};
const STAGES=['UPLOAD','TOPIC','ENTITY','PROPERTY','RELATION','REVIEW'];
const STAGE_LABEL=['上传资料','概要主题','实体构建','属性构建','关系构建','审核发布'];
const KINDS={entity:'实体',attribute:'属性',relation:'关系'};
const KIND_TABS={entity:'entity',attribute:'attribute',relation:'relation'};
const TABS={overview:'TOPIC',entity:'ENTITY',attribute:'PROPERTY',relation:'RELATION',review:'REVIEW'};
// Restoring or reopening a workspace must honor its persisted workflow stage.
function tabForStage(stage){return ({UPLOAD:'overview',TOPIC:'overview',ENTITY:'entity',PROPERTY:'attribute',RELATION:'relation',REVIEW:'review'})[stage]||'overview';}

function toast(msg,ms=3800){let el=$('#toast');el.textContent=msg;el.classList.remove('hidden');clearTimeout(window._toastTimer);window._toastTimer=setTimeout(()=>el.classList.add('hidden'),ms)}
async function api(path, options={}){
 const headers={'X-Actor':S.actor,...(S.token?{'X-API-Key':S.token}:{})};
 if(options.body && !(options.body instanceof FormData))headers['Content-Type']='application/json';
 const response=await fetch('/api'+path,{...options,headers:{...headers,...(options.headers||{})}});
 const type=response.headers.get('content-type')||'';
 if(!response.ok){let msg=String(response.status);try{let data=await response.json();msg=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{};throw new Error(msg)}
 return type.includes('json')?response.json():response.text();
}
async function task(fn){if(S.busy)return;S.busy=true;document.body.style.cursor='progress';try{await fn()}catch(e){toast('操作失败：'+e.message,5500);console.error(e)}finally{S.busy=false;document.body.style.cursor=''}}
async function reloadProjects(){S.projects=await api('/projects');renderProjects()}
async function loadProject(id,{activateStage=false}={}){
 clearTimeout(S.jobPollTimer);S.jobPollTimer=null;
 const switched=S.id!==id||!S.view;
 const previousStage=S.view?.project?.stage;
 const followingStage=previousStage!=null&&S.tab===tabForStage(previousStage);
 if(switched){S.batchSelections={entity:new Set(),attribute:new Set(),relation:new Set()};S.pendingData={};S.pendingPage={entity:1,attribute:1,relation:1};}
 S.id=id;S.view=await api('/projects/'+id);
 // On first load, workspace switch, or explicit re-entry, activate the persisted stage.
 // Ordinary refresh / job polling keeps a user's manually selected read-only tab.
 if(switched||activateStage||(followingStage&&previousStage!==S.view.project.stage))S.tab=tabForStage(S.view.project.stage);
 await fetchPendingPage(S.tab,false);
 renderAll();resumeJobs();
}
function progressTitle(phase){return ({queued:'等待处理',preparing:'读取准备',parsing:'文档解析',reading:'正在阅读原文',summarizing:'提炼文档概要',discovering:'识别本体线索',aligning:'本体对齐与候选校验',extracting:'候选生成',merging:'跨章节内容归并',finalizing:'综合分析',retry_wait:'等待自动恢复',reviewing:'跨页全部同意',clarifying:'需要人工协商',partial:'部分完成 · 存在待处理来源',succeeded:'处理完成',complete:'处理完成',failed:'处理失败'})[phase]||'正在处理';}
function progressEta(sec){if(sec==null)return '模型耗时尚不稳定，暂不估时';if(sec<=0)return '已完成';if(sec<60)return `预计还需约 ${Math.max(1,Math.ceil(sec/10)*10)} 秒`;if(sec<3600)return `预计还需约 ${Math.ceil(sec/60)} 分钟`;return `预计还需约 ${(sec/3600).toFixed(1)} 小时`;}
function recentJobProgress(job){return (S.view?.assistant_progress||[]).filter(x=>x.job_id===job?.id).at(-1)||null;}
function jobMarkup(job){
 if(!job)return '<div class="notice info" role="status">提交后，右侧「本体协商助手」会持续报告正在阅读的资料、发现和处理进度。</div>';
 const running=['queued','running'].includes(job.state);
 const error=['failed','interrupted'].includes(job.state);
 const ev=recentJobProgress(job);
 if(job.job_kind==='review_all'){
  const report=parseJobResult(job);
  const pct=job.total_units?Math.floor(100*job.completed_units/job.total_units):0;
  return `<div class="job-status" role="status" aria-live="polite"><div class="job-head"><strong>跨页全部同意${running?' · 处理中':' · 已结束'}</strong><span>${job.completed_units}/${job.total_units} 条 · ${pct}%</span></div><div class="job-progress"><div style="width:${pct}%"></div></div><p>${esc(job.detail)}</p><small>成功 ${report.succeeded||0} · 失败 ${report.failed||0}；失败项保留，只修改本地草稿。</small>${actionButton('查看本次审核结果','reviewallreport:'+job.id,'neutral')}</div>`;
 }
 const pct=ev?.percent ?? (job.state==='succeeded'?100:0);
 const title=job.state==='partial'?progressTitle('partial'):job.state==='succeeded'?'处理完成':ev?progressTitle(ev.phase):running?'等待任务启动':error?'任务失败':'任务已完成';
 return `<div class="job-status ${error?'job-error':''}" role="status" aria-live="polite">
  <div class="job-head"><strong>${esc(title)}</strong><span>${pct?`${pct}% · `:''}${ev?.eta_seconds!=null?esc(progressEta(ev.eta_seconds)):'等待进一步估算'}</span></div>
  <div class="job-progress"><div style="width:${Math.min(100,pct)}%"></div></div>
  <div class="job-detail">${esc(ev?.content||job.detail||'等待开始')}</div>
  ${job.error?`<div class="job-error-text">${esc(job.error)}</div>`:''}
  ${!running?jobCostMarkup(job):''}
  ${running?'<div class="job-tip">实时过程和可追溯的阶段发现会同步输出到右侧本体协商助手。</div>':''}
 </div>`;
}
function jobCostMarkup(job){
 const data=parseJobResult(job),m=data.metrics;
 if(!m)return '<div class="job-tip">此任务没有完整调用统计；不据此判断是否调用模型。</div>';
 return `<div class="job-tip">实际模型请求 ${m.llm_requests||0} 次 · 原文缓存命中 ${m.source_cache_hits||0} · 当前意图建议缓存命中 ${m.interpretation_cache_hits||0}<br>原文缓存不代表完成了本轮语义理解；新目标与用户决定使用独立的建议缓存。</div>`;
}

async function showModelingIntent(){
 if(!S.id){toast('请先选择工作空间');return;}
 const pid=S.id,r=await api(`/projects/${pid}/modeling-context`),c=r.context;
 const list=(c.include_types||[]).join('\n'),omit=(c.exclude_types||[]).join('\n');
 const history=(r.history||[]).slice(0,8).map(x=>`<div class="intent-history"><b>用户决定 v${x.version}</b><small>${esc(x.created_at)}</small><p>${esc(x.note||'确认建模目标、范围和策略')}</p></div>`).join('');
 modal('建模意图与用户决定',`<div class="notice info">文件格式只决定如何读取，不决定必须建什么本体。以下内容作为用户决定保存，原文和已审核本体不会被重写。</div>
 <div class="field"><label>本次希望构建什么业务模型？</label><textarea class="control" id="intentGoal" rows="3" maxlength="2000" placeholder="例如：只梳理报警受理过程中的通用类型和角色，不录入具体案件">${esc(c.goal)}</textarea></div>
 <div class="split"><div class="field"><label>明确纳入的类型（可留空；每行一个精确名称）</label><textarea class="control" id="intentInclude" rows="4">${esc(list)}</textarea></div><div class="field"><label>明确排除的类型（可留空；每行一个精确名称）</label><textarea class="control" id="intentExclude" rows="4">${esc(omit)}</textarea></div></div>
 <p class="smalltext">留空不限制。精确名单用于候选筛选；自然语言目标供模型理解，不会凭关键词自动裁掉原文。</p>
 <div class="field"><label>业务角色怎样表达？</label><select class="control" id="intentRole"><option value="ask" ${c.role_strategy==='ask'?'selected':''}>遇到角色歧义先询问我</option><option value="relations" ${c.role_strategy==='relations'?'selected':''}>优先复用自然人，以行为关系表达角色</option><option value="types" ${c.role_strategy==='types'?'selected':''}>允许单独提出角色类型；继承仍需审核</option></select></div>
 <div class="field"><label>理解方式</label><select class="control" id="intentReading"><option value="auto" ${c.reading_mode==='auto'?'selected':''}>自动：明确结构先提取，不确定内容交给模型/用户</option><option value="semantic_review" ${c.reading_mode==='semantic_review'?'selected':''}>语义复核：表格、章节也需模型复核（需要已配置模型）</option></select></div>
 <div class="field"><label>人工补充的术语或建模要求（不是原文证据）</label><textarea id="intentInstructions" class="control" rows="3" maxlength="4000">${esc(c.instructions)}</textarea></div>
 <div class="field"><label>本次确认说明</label><input id="intentNote" class="control" maxlength="2000" placeholder="例如：审核人只作为角色，不新增独立自然人实例"></div>
 <div class="notice warn">改变语义范围后，旧候选会标为需复核；进行中的旧上下文任务会停止提交。已审核本体保留，不自动发布或同步。</div><details><summary>最近的用户决定（可追溯）</summary>${history||'<p>尚未保存独立建模决定。</p>'}</details>`,[
  {text:'取消',click:closeModal},{text:'确认并保存用户决定',class:'primary',click:()=>task(async()=>{
    const terms=selector=>$(selector).value.split(/[\n,，;；]/).map(x=>x.trim()).filter(Boolean);
    await api(`/projects/${pid}/modeling-context`,{method:'PUT',body:JSON.stringify({expected_version:c.version,
      goal:$('#intentGoal').value.trim(),include_types:terms('#intentInclude'),exclude_types:terms('#intentExclude'),
      role_strategy:$('#intentRole').value,reading_mode:$('#intentReading').value,
      instructions:$('#intentInstructions').value.trim(),note:$('#intentNote').value.trim()})});
    closeModal();if(S.id===pid)await loadProject(pid);toast('建模决定已保存，原文与已审核本体未改写；重新生成或复核相关候选。',7000);
  })}
 ]);
}

async function showSupportedFormats(){
 const info=await api('/formats'),available=(info.formats||[]).filter(f=>f.available),other=(info.formats||[]).filter(f=>!f.available);
 const table=items=>`<div style="overflow:auto"><table class="data-table"><thead><tr><th>扩展名</th><th>格式</th><th>读取边界</th></tr></thead><tbody>${items.map(f=>`<tr><td><code>${esc(f.extension)}</code></td><td>${esc(f.label||f.extension)}<br><small>${f.available?'可读取':f.adapter==='optional_converter'?'需转换/另行验证':'缺少依赖'}</small></td><td>${esc(f.note||'')}${!f.available&&f.requires?'<br><small>组件：'+esc(f.requires)+'</small>':''}</td></tr>`).join('')}</tbody></table></div>`;
 modal('支持格式与实际读取边界',`<div class="notice info">${esc(info.notice)}</div><h4>当前可读取 · ${available.length} 个扩展名</h4>${table(available)}<details><summary>尚需组件或转换验证 · ${other.length} 个扩展名</summary>${table(other)}</details><div class="notice warn">不执行公式/SQL/脚本，不自动 OCR；ZIP 仅接受 OpenMind 原生导出，不把任意压缩包当文本。</div>`,[{text:'关闭',click:closeModal}]);
}

async function showReadingBoundaries(){
 if(!S.id)return;
 const r=await api(`/projects/${S.id}/understanding`),m=r.materials;
 modal('材料阅读边界 · 不把部分读取当作完整理解',`<div class="notice info">${esc(r.notice)}</div><p>已读取 ${m.total_units} 个文字单元；${m.unverified_source_parts} 项来源/版式边界需核查。</p>${m.documents.map(d=>`<div class="panel"><h4>${esc(d.filename)}</h4><p>${esc(d.observed_profile)} · ${d.units} 个单元</p><small>${esc(JSON.stringify(d.kinds))}<br>读取器：${esc((d.readers||[]).join('、')||'旧版记录')} ${d.encodings?.length?' · 编码：'+esc(d.encodings.join('、')):''}</small>${d.contains_tentative_language?'<p class="smalltext">检测到建议、示例或假设用语：不自动认定为定稿。</p>':''}${d.warnings.map(w=>`<div class="notice warn">${esc(w)}</div>`).join('')||'<p>未检测到解析器已知的覆盖警告；这不等于语义无遗漏。</p>'}</div>`).join('')}`,[{text:'关闭',click:closeModal}]);
}

function recheckIntent(qid){
 const pid=S.id,rev=S.view.project.revision;
 modal('按当前建模意图人工复核',`<div class="notice warn">这不是自动重新推理。请先核对候选与当前目标，再记录结论；本次只更新复核依据，候选仍待审批。</div><div class="field"><label>你的复核理由（至少 2 字）</label><textarea id="intentRecheckNote" class="control" rows="4"></textarea></div>`,[
 {text:'取消',click:closeModal},{text:'已核对，记录为用户决定',class:'primary',click:()=>task(async()=>{
   await api(`/projects/${pid}/proposals/${qid}/recheck-intent`,{method:'POST',body:JSON.stringify({expected_revision:rev,note:$('#intentRecheckNote').value.trim()})});
   closeModal();if(S.id===pid)await loadProject(pid);toast('人工复核依据已保存；仍需点击接受才写入本体草稿。');
 })}]);
}

function resumeJobs(){
 clearTimeout(S.jobPollTimer);S.jobPollTimer=null;
 if(!S.view)return;
 const jobs=S.view.jobs_latest||{};
 const active=[...(S.view.active_jobs||[]),jobs.review_all,jobs.parse,jobs.summary,jobs.ENTITY,jobs.PROPERTY,jobs.RELATION].find(x=>x&&['queued','running'].includes(x.state));
 if(active)pollJob(S.id,active.id);
}
function pollJob(pid,jid){
 S.jobPollTimer=setTimeout(async()=>{
  if(S.id!==pid)return;
  try{
   const existing=(S.view.assistant_progress||[]).filter(e=>e.job_id===jid);
   const since=existing.length?existing.at(-1).created_at:'';
   const r=await api(`/projects/${pid}/jobs/${jid}${since?'?since='+encodeURIComponent(since):''}`),job=r.job;
   if(!job||S.id!==pid)return;
   const jobKey=job.job_kind==='review_all'?'review_all':job.job_kind==='parse'?'parse':job.job_kind==='summary'?'summary':job.stage;
   const previousCompleted=S.view.jobs_latest[jobKey]?.completed_units;
   S.view.jobs_latest[jobKey]=job;
   S.view.active_jobs=(S.view.active_jobs||[]).filter(j=>j.id!==jid);
   if(['queued','running'].includes(job.state))S.view.active_jobs.push(job);
   if(['entity','attribute','relation'].includes(S.tab)&&previousCompleted!==job.completed_units){await fetchPendingPage(S.tab,false);renderContent();}
   const known=new Map((S.view.assistant_progress||[]).map(x=>[x.id,x]));
   for(const event of (r.assistant_events||[]))known.set(event.id,event);
   S.view.assistant_progress=[...known.values()].sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)).slice(-150);
   const el=$(job.job_kind==='review_all'?'#currentReviewAllProgress':job.job_kind==='parse'?'#currentParseProgress':'#currentJobProgress');if(el){el.innerHTML=jobMarkup(job);el.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>task(()=>action(b.dataset.action)));}
   renderChat();
   if(['queued','running'].includes(job.state)){pollJob(pid,jid);return;}
   if(job.job_kind==='review_all'){const report=parseJobResult(job);toast(`跨页审核结束：成功 ${report.succeeded||0}，失败 ${report.failed||0}，未处理 ${report.unprocessed||0}`,8000);}
   else if(job.state==='succeeded')toast('后台任务已完成，请审核结果与原文证据。',6000);
   else toast('后台任务未完成：'+(job.error||job.detail),8000);
   await loadProject(pid);await reloadProjects();
  }catch(err){
    const el=$('#currentJobProgress');if(el)el.innerHTML=`<div class="notice warn">读取后台进度失败：${esc(err.message)}。稍后重试或刷新页面。</div>`;
    if(S.id===pid)pollJob(pid,jid);
  }
 },1400);
}
async function refresh(){await reloadProjects();if(S.id)await loadProject(S.id);else renderAll();await refreshEngineStatus()}
async function refreshEngineStatus(){const status=await api('/settings/model');$('#modeIndicator').textContent=status.enabled?'模型已配置（按需调用）：'+status.model:'规则演示模式（非语义 AI）';}
async function fetchPendingPage(kind,rerender=true){
 if(!kind||!['entity','attribute','relation'].includes(kind)||!S.id)return;
 const page=S.pendingPage[kind]||1;
 const r=await api(`/projects/${S.id}/proposals/page?kind=${kind}&status=pending&page=${page}&page_size=${S.pageSize}`);
 S.pendingData[kind]=r;
 // Selection never survives a page transition.
 const ids=new Set(r.items.map(x=>x.id));
 for(const id of S.batchSelections[kind])if(!ids.has(id))S.batchSelections[kind].delete(id);
 if(rerender)renderContent();
}
async function changeCandidatePage(kind,delta){
 const r=S.pendingData[kind];if(!r)return;
 const last=Math.max(1,Math.ceil(r.total/S.pageSize));
 const next=Math.max(1,Math.min(last,S.pendingPage[kind]+delta));
 if(next===S.pendingPage[kind])return;
 const previous=S.pendingPage[kind];S.pendingPage[kind]=next;S.batchSelections[kind].clear();
 try{await fetchPendingPage(kind)}catch(err){S.pendingPage[kind]=previous;throw err}
}
function renderProjects(){
 const list=$('#projectList');
 list.innerHTML=S.projects.length?S.projects.map(p=>`<div class="project-row"><button class="project-item ${S.id===p.id?'current':''}" data-project="${esc(p.id)}"><span class="project-icon">◈</span><span><strong>${esc(p.name)}</strong><small>${esc(STAGE_LABEL[STAGES.indexOf(p.stage)])} · 修订 ${p.revision}</small></span></button><button class="project-delete" type="button" data-delete-project="${esc(p.id)}" title="删除工作空间" aria-label="删除工作空间 ${esc(p.name)}">✕</button></div>`).join(''):'<p class="smalltext" style="padding:10px;color:#829ec8">尚未创建项目</p>';
 list.querySelectorAll('[data-project]').forEach(el=>el.onclick=()=>task(async()=>{await loadProject(el.dataset.project,{activateStage:true})}));
 list.querySelectorAll('[data-delete-project]').forEach(el=>el.onclick=e=>{e.stopPropagation();const p=S.projects.find(x=>x.id===el.dataset.deleteProject);if(p)confirmDeleteProject(p)});
}
function confirmDeleteProject(p){
 modal('删除工作空间 · 不影响 OpenMind',`<div class="notice warn"><b>不可恢复：</b>将永久删除工作空间及其本地文档、候选、审核记录、任务和发布版本。<br><b>不会删除或修改 OpenMind 远端分支、本体及其他工作空间。</b></div><div class="field"><label>确认删除：请完整输入「${esc(p.name)}」</label><input id="deleteConfirmName" class="control" autocomplete="off" placeholder="输入工作空间名称"></div>`,[
  {text:'取消',click:closeModal},
  {text:'永久删除',class:'danger',click:()=>task(async()=>{
    const name=$('#deleteConfirmName')?.value||'';
    if(name!==p.name)throw Error('输入的工作空间名称不匹配');
    const r=await api(`/projects/${p.id}`,{method:'DELETE',body:JSON.stringify({confirm_name:name,expected_revision:p.revision})});
    closeModal();
    if(S.id===p.id){clearTimeout(S.jobPollTimer);S.jobPollTimer=null;S.id=null;S.view=null;S.tab='overview';S.pendingData={};S.batchSelections={entity:new Set(),attribute:new Set(),relation:new Set()};}
    await reloadProjects();renderAll();toast(r.file_cleanup_pending?'工作空间已删除；部分上传文件需要管理员清理':'工作空间已删除（OpenMind 远端未修改）',6500);
  })}
 ]);
}

function renderAll(){const v=S.view;$('#workspace').classList.toggle('hidden',!v);$('#welcome').classList.toggle('hidden',!!v);if(!v){$('#currentTitle').textContent='请选择项目';return}const p=v.project;$('#currentTitle').textContent=p.name;$('#projectName').textContent=p.name;$('#projectSub').textContent=`当前：${STAGE_LABEL[STAGES.indexOf(p.stage)]} · 修订 ${p.revision} · 已发布 v${p.published_version}`;let engine=v.engine.mode==='LLM'?'已启用模型 '+v.engine.model:'规则演示模式（非语义 AI）';$('#modeIndicator').textContent=engine;$('#assistantNotice').textContent=v.engine.mode==='LLM'?'AI 可以解释资料、讨论候选，但只能创建待审批建议。':'当前未配置模型：规则仅识别“实体：… / 属性：… / 关系：…”格式；自然语言协商会被记录，但不会自动理解。';renderProjects();renderStepper();renderTabs();renderContent();renderChat()}
function renderStepper(){let idx=STAGES.indexOf(S.view.project.stage);$('#stepper').innerHTML=STAGES.map((s,i)=>`<div class="step ${i===idx?'active':i<idx?'past':''}"><span class="number">${i<idx?'✓':i+1}</span><span>${STAGE_LABEL[i]}</span></div>`).join('')}
function renderTabs(){document.querySelectorAll('.tab').forEach(el=>{el.classList.toggle('current',el.dataset.tab===S.tab);el.onclick=()=>{S.tab=el.dataset.tab;renderTabs();renderContent();if(["entity","attribute","relation"].includes(S.tab))task(()=>fetchPendingPage(S.tab))}})}
function renderContent(){if(!S.view)return;let c=$('#tabContent');if(S.tab==='overview')c.innerHTML=renderOverview();if(['entity','attribute','relation'].includes(S.tab))c.innerHTML=renderModelTab(S.tab);if(S.tab==='review')c.innerHTML=renderReview();wireContent()}
function actionButton(label,cmd,sty='primary',extra=''){return `<button class="button ${sty} small" data-action="${cmd}" ${extra}>${label}</button>`}
function renderOverview(){const v=S.view,p=v.project,base=v.openmind_baseline;
const baselinePanel=v.openmind_bound?`<div class="panel"><div class="subhead"><div><h3>OpenMind 已有分支本体 · 本轮构建基线</h3><p>先复用分支中的类型、属性和关系，再从文档查找需要补充的定义。已有对象只读，不会重复创建。</p></div><div class="toolbar">${actionButton('刷新分支基线','baselineRefresh','neutral')}</div></div>
${base?`<div class="stats"><div class="stat"><strong>${base.entity_count}</strong><small>既有实体</small></div><div class="stat"><strong>${base.attribute_count}</strong><small>既有属性</small></div><div class="stat"><strong>${base.relation_count}</strong><small>既有关系</small></div></div><div class="doc-info">分支 ${esc(base.branch_id)} · 最近读取 ${esc(base.refreshed_at)}</div><div class="toolbar">${actionButton('查看原有实体','baselineList:entity','neutral')}${actionButton('查看原有属性','baselineList:attribute','neutral')}${actionButton('查看原有关系','baselineList:relation','neutral')}</div>`:'<div class="notice warn">已绑定 OpenMind，但尚未读取完整分支目录。请先刷新基线，否则禁止单独按文档构建。</div>'}</div>`:'';
return `
 <div class="subhead"><div><h2>01 / 先理解资料，再确定本体主题</h2><p>不直接建模。先核对材料摘要、对象范围和不应纳入的内容。</p></div></div>
 ${baselinePanel}
 <div id="currentParseProgress">${jobMarkup(v.jobs_latest?.parse)}</div>
 <div class="upload-area" id="dropArea"><div><b>⇧ 导入文件或拖拽到这里</b><p>TXT / MD / JSON / JSONL / YAML / XML / HTML / CSV / EML / DOCX / XLSX / PPTX / PDF 文字层；格式不代表建模含义</p></div><div><button class="button neutral small" data-action="formats">格式与读取边界</button> <button class="button neutral small" data-action="chooseFiles">选择文件</button></div><input id="fileInput" class="hidden" type="file" multiple></div>
 <div class="panel"><h3>已导入文档 <span class="chip">${v.documents.length} 个</span></h3>${v.documents.length?v.documents.map(d=>`<div class="doc-row"><div style="display:flex;gap:10px;align-items:center"><span class="doc-icon">▤</span><div><div class="doc-name">${esc(d.filename)}</div><div class="doc-info">${Math.max(1,Math.round(d.bytes/1024))} KB · SHA256 ${esc(d.sha256.slice(0,12))}…</div></div></div>${actionButton('原文证据','chunks:'+d.id,'neutral')}</div>`).join(''):'<div class="empty">尚无文档。请先上传材料以开始完整闭环。</div>'}</div>
 <div class="actionbar"><div class="hint">文档仅作为增量证据；已绑定 OpenMind 时必须先加载已有实体、属性和关系。</div>${actionButton('↻ 重新解析已有文件','reparse','neutral',v.documents.length?'':'disabled')}${actionButton('✳ 生成文档概要与建议主题','analyze','primary',v.documents.length&&(!v.openmind_bound||base)&&!['queued','running'].includes(v.jobs_latest?.summary?.state)?'':'disabled')}</div>
 <div id="currentJobProgress">${jobMarkup(v.jobs_latest?.summary)}</div>
 <div class="panel intent-panel"><h3>建模意图与阅读边界</h3><p>${esc(v.modeling_context?.goal||'建模目标默认沿用下方经人工确认的主题与边界。')}</p><div class="candidate-basis"><span class="chip green">用户决定 v${v.modeling_context?.version||0}</span><span class="smalltext">不把用户的建模要求冒充原文事实</span></div><div class="toolbar">${actionButton('设置目标、范围与角色策略','modelingIntent','neutral')}${actionButton('查看材料阅读边界','readingBoundaries','neutral')}</div></div>
 <div class="panel tinted"><h3>文档概要 · 人工可修改</h3><div class="field"><label>内容概述</label><textarea id="summaryInput" class="control" rows="7" placeholder="先通过“生成概要”，再根据实际资料修订">${esc(p.candidate_summary||p.summary)}</textarea></div><div class="field"><label>本体对象主题与边界</label><textarea id="scopeInput" class="control" rows="5" placeholder="例：以网关设备为主题，只建立设备、组件、模块及结构关系">${esc(p.candidate_scope||p.scope)}</textarea></div>${p.scope_confirmed?'<div class="notice ok">✓ 当前主题已审核通过。可以继续实体构建，重新编辑并保存仍会重新确认。</div>':'<div class="notice warn">需要人工确认建模主题，确认后才允许创建候选本体。</div>'}<div style="display:flex;justify-content:flex-end;gap:9px">${actionButton('✓ 确认主题，进入实体构建','confirmTopic','primary',v.documents.length&&(!v.openmind_bound||base)?'':'disabled')}</div></div>
 `}
function modelName(id){return S.view.snapshot.entities.find(e=>e.id===id)?.name||id||'（未知）'}
function relationDisplay(p){return p.name||({subClassOf:'继承自',hasPart:'包含部件',partOf:'属于整体',relatedTo:'关联',hasComponent:'包含组件',locatedAt:'位于',connectsTo:'连接到',dependsOn:'依赖'})[p.predicate]||'待补充中文名称';}
function codeLine(p){return `<div class="definition-code">英文 CODE：<code>${esc(p.code||'接受前自动建议，可编辑')}</code></div>`;}
function proposalTitle(q){let p=q.payload;if(p.operation==='delete')return '删除已确认'+KINDS[q.kind]+' · '+esc(p.record_id||p.target_id);if(p.operation==='update')return '修改 · '+esc(q.kind==='relation'?relationDisplay(p):p.name);if(q.kind==='entity')return esc(p.name)+' <span class="chip">概念类</span>';if(q.kind==='attribute')return esc(modelName(p.owner_id))+' · '+esc(p.name);return esc(modelName(p.source_id))+' → '+esc(relationDisplay(p))+' → '+esc(modelName(p.target_id));}

function evidenceHtml(items){if(!items||!items.length)return '<div class="evidence">无可核验原文证据；请人工核实来源及定义。</div>';return items.slice(0,3).map(ev=>`<div class="evidence"><span class="chip ${ev.verified?'green':'orange'}">${ev.verified?'原文匹配':'未核实'}</span> ${esc(ev.locator)} · ${esc(ev.document_id)}<br>${esc(ev.quote)}</div>`).join('')}
function basisMarkup(q){
 const b=q.basis||{kind:'legacy',label:'历史候选（未分类）'};
 const color=b.kind==='source_explicit'?'blue':b.kind==='user_decision'?'green':'orange';
 return `<div class="candidate-basis"><span class="chip ${color}">${esc(b.label||'建模推断')}</span><small>${esc(b.rationale||'旧版未记录来源类型，请结合原文审核')}</small></div>${b.context_stale?'<div class="notice warn">建模意图或目标分支已改变。这条旧候选需重新生成，或先人工复核；不能直接全部同意。</div>':''}`;
}
function renderProposal(q){return `<div class="proposal-card"><div class="proposal-top"><label class="proposal-select"><input type="checkbox" data-batch-id="${esc(q.id)}" data-batch-kind="${esc(q.kind)}" ${S.batchSelections[q.kind].has(q.id)?'checked':''} aria-label="选择候选 ${esc(q.payload.name||q.payload.predicate||q.id)}"></label><div class="proposal-body"><div class="proposal-title">${proposalTitle(q)}</div>${q.payload.operation==='delete'?'':codeLine(q.payload)}<div class="proposal-description">${esc(q.stage)} · ${esc(q.payload.operation||'add')} · ${esc(q.payload.description||'等待审核说明')}</div></div><span class="chip orange">待确认</span></div>${basisMarkup(q)}${evidenceHtml(q.evidence)}<div class="proposal-actions">${q.basis?.context_stale?actionButton('按当前意图复核','recheckIntent:'+q.id,'neutral'):actionButton('✓ 接受','approve:'+q.id,'success')}${q.payload.operation==='delete'?'':actionButton('✎ 修改后接受','edit:'+q.id,'neutral',q.basis?.context_stale?'disabled':'')}${actionButton('✕ 拒绝','reject:'+q.id,'danger')}</div></div>`}

function renderModelTab(tab){const v=S.view,p=v.project;let kind=tab==='entity'?'entity':tab==='attribute'?'attribute':'relation';let arr=v.snapshot[tab==='entity'?'entities':tab==='attribute'?'attributes':'relations'];let pending=S.pendingData[kind]?.items||[];let pendingTotal=S.pendingData[kind]?.total??(v.pending_totals?.[kind]||0);let selection=S.batchSelections[kind];let pendingIds=new Set(pending.map(q=>q.id));for(const id of selection)if(!pendingIds.has(id))selection.delete(id);let available=p.scope_confirmed;let isActive=p.stage===KIND_TAB_STAGE(kind);let prev=STAGES.indexOf(p.stage)>2?STAGES[STAGES.indexOf(p.stage)-1]:null;return `<div class="subhead"><div><h2>${tab==='entity'?'02 / 实体与概念':tab==='attribute'?'03 / 属性与约束':'04 / 关系与层级'}</h2><p>${tab==='entity'?'从业务实例归纳本体实体类型与角色，不创建任何实例。':tab==='attribute'?'将属性归属到已经确认的实体。':'明确关系方向、语义、层级，禁止循环。'}</p></div><div class="toolbar">${actionButton('＋ 手动新增','create:'+kind,'neutral',isActive?'':'disabled title="请先进入对应阶段"')}${actionButton('✳ 生成/继续候选','propose','primary',isActive&&![...(v.active_jobs||[]),v.jobs_latest?.[p.stage]].some(j=>j&&['queued','running'].includes(j.state)&&['propose','gap_retry'].includes(j.job_kind))?'':'disabled title="任务进行中或未进入对应阶段"')}</div></div>
 ${v.openmind_baseline?`<div class="notice info">当前以 OpenMind 分支为基线：${v.openmind_baseline.entity_count} 个既有实体、${v.openmind_baseline.attribute_count} 个既有属性、${v.openmind_baseline.relation_count} 个既有关系。新候选会优先引用已有类型，不重复创建。${actionButton('查看远端已有'+KINDS[kind],'baselineList:'+kind,'neutral')}</div>`:v.openmind_bound?'<div class="notice warn">尚未加载 OpenMind 分支基线，无法开始候选构建。</div>':''}
 <div id="currentJobProgress">${jobMarkup(v.jobs_latest?.[KIND_TAB_STAGE(kind)])}</div>
 ${v.proposal_progress?.[p.stage]?.total_batches>1?`<div class="notice info">分批文档覆盖进度：${v.proposal_progress[p.stage].processed_batches}/${v.proposal_progress[p.stage].total_batches} 批。${v.proposal_progress[p.stage].processed_batches<v.proposal_progress[p.stage].total_batches?'后台会自动连续处理后续章节。':'本阶段已遍历所有已解析文本批次；仍需人工检查候选完整性。'}</div>`:''}
 ${!available?'<div class="notice warn">请先上传文档并确认概要主题。</div>':!isActive?'<div class="notice info">当前实际工作流阶段为“'+STAGE_LABEL[STAGES.indexOf(p.stage)]+'”。可以查看已确认本体；新增、修改及自动抽取需要先切换到本阶段。</div>':''}
 <div class="stats"><div class="stat"><strong>${v.snapshot.entities.length}</strong><small>已确认实体</small></div><div class="stat"><strong>${v.snapshot.attributes.length}</strong><small>已确认属性</small></div><div class="stat"><strong>${v.snapshot.relations.length}</strong><small>已确认关系</small></div><div class="stat"><strong>${pendingTotal}</strong><small>此类待审核</small></div></div>
 ${v.extraction_gaps?.filter(g=>g.stage===KIND_TAB_STAGE(kind)).length?`<div class="notice warn"><strong>需要人工处理的抽取缺口（显示最近 20 条）</strong>${v.extraction_gaps.filter(g=>g.stage===KIND_TAB_STAGE(kind)).slice(0,20).map(g=>`<div class="gap-item">批次 ${g.batch_number}: ${esc(g.reason)}</div>`).join('')}<div class="job-tip">缺口包含模型无法通过结构校验的候选，未被静默丢弃。</div></div>`:''}
 <div class="panel"><div class="subhead"><h3>原文抽取覆盖审计</h3>${actionButton('检查章节与批次','coverage:'+KIND_TAB_STAGE(kind),'neutral')}</div><p class="section-subtitle">每个批次都记录实际输入、候选数量、校验缺口；重点检查零候选批次。已扫描不等于所有实体、属性和关系都被识别。</p></div>
 <div class="countline">人工审核候选共 ${pendingTotal} 条 · 当前页 ${pending.length} 条 <span class="chip orange">模型建议不会自动生效</span></div>
 ${v.jobs_latest?.review_all?.stage===KIND_TAB_STAGE(kind)?`<div id="currentReviewAllProgress">${jobMarkup(v.jobs_latest.review_all)}</div>`:''}
 ${pending.length?`<div class="batch-review-toolbar" role="group" aria-label="批量审核${KINDS[kind]}候选"><label><input type="checkbox" data-batch-select-all="${kind}" aria-label="全选当前${KINDS[kind]}候选"> 全选本页</label><span data-batch-count="${kind}">已选择 ${selection.size} / ${pending.length} 条</span><button class="button success small" data-action="bulk:${kind}:accept" data-batch-command="${kind}" ${selection.size?'':'disabled'}>✓ 批量同意</button><button class="button danger small" data-action="bulk:${kind}:reject" data-batch-command="${kind}" ${selection.size?'':'disabled'}>✕ 批量拒绝</button><button class="button primary small review-all-button" data-action="allaccept:${kind}" ${isActive&&available&&!isReviewAllRunning(kind)?'':'disabled'}>✓ 全部同意（跨页 ${pendingTotal} 条）</button></div>`:''}
 ${pending.length?pending.map(renderProposal).join(''):'<div class="empty">本页没有待审核候选。你可以跳过建议，继续下一阶段。</div>'}
 <div class="actionbar"><span class="hint">待审核总计 ${pendingTotal} 条 · 第 ${S.pendingPage[kind]} / ${Math.max(1,Math.ceil(pendingTotal/S.pageSize))} 页；“批量同意”仅本页，“全部同意”覆盖本阶段所有页面</span>
 <div class="toolbar">${actionButton('← 上一页','page:'+kind+':-1','neutral',S.pendingPage[kind]>1?'':'disabled')}${actionButton('下一页 →','page:'+kind+':1','neutral',S.pendingPage[kind]<Math.ceil(pendingTotal/S.pageSize)?'':'disabled')}</div></div>
 <div class="countline">正式草稿 · 已确认 ${arr.length} 项</div>
 ${tab==='entity'?renderGraph():''}
 ${arr.length?`<div class="table-scroll"><table class="data-table"><thead><tr>${tab==='entity'?'<th>类型</th><th>实体名称</th><th>定义</th><th>操作</th>':tab==='attribute'?'<th>所属实体</th><th>属性</th><th>数据类型</th><th>约束</th><th>操作</th>':'<th>源实体</th><th>关系</th><th>目标实体</th><th>操作</th>'}</tr></thead><tbody>${arr.map(x=>tab==='entity'?`<tr><td><span class="chip">${x.kind==='class'?'概念':'实例'}</span></td><td><b>${esc(x.name)}</b>${codeLine(x)} ${x.origin==='openmind'?'<span class="chip green">OpenMind 已存在</span>':''}</td><td>${esc(x.description||'—')}</td><td>${modelOps(kind,x,isActive)}</td></tr>`:tab==='attribute'?`<tr><td>${esc(modelName(x.owner_id))}</td><td><b>${esc(x.name)}</b>${codeLine(x)}</td><td>${esc(x.datatype)}</td><td>${x.required?'必填':''} ${x.multivalued?'多值':''}</td><td>${modelOps(kind,x,isActive)}</td></tr>`:`<tr><td>${esc(modelName(x.source_id))}</td><td><span class="chip">${esc(relationDisplay(x))}</span>${codeLine(x)}</td><td>${esc(modelName(x.target_id))}</td><td>${modelOps(kind,x,isActive)}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">此阶段还没有已确认的数据。</div>'}
 <div class="actionbar"><div class="hint">可以回到以前的阶段补充遗漏，草稿不会被覆盖。</div><div class="toolbar">${prev?actionButton('↶ 返回上一阶段','stage:'+prev,'neutral'):''}${isActive?actionButton('进入下一阶段 →','stage:'+STAGES[STAGES.indexOf(p.stage)+1],'primary'):''}</div></div>`}
function modelOps(kind,x,isActive){if(x.origin==='openmind')return '<span class="chip green">既有定义 · 只读复用</span>';return actionButton('修改','revise:'+kind+':'+x.id,'neutral',isActive?'':'disabled')+' '+actionButton('删除','remove:'+kind+':'+x.id,'danger',isActive?'':'disabled')}
function KIND_TAB_STAGE(kind){return {entity:'ENTITY',attribute:'PROPERTY',relation:'RELATION'}[kind]}
function renderGraph(){let es=S.view.snapshot.entities,rs=S.view.snapshot.relations;if(!es.length)return '';let width=620,height=315,centerX=310,centerY=160, radius=Math.min(115+es.length*5,135), pos={}; es.forEach((e,i)=>{let a=(i/es.length)*Math.PI*2-Math.PI/2;pos[e.id]={x:centerX+Math.cos(a)*radius*(es.length===1?0:1),y:centerY+Math.sin(a)*radius*(es.length===1?0:1)}});
let edges=rs.filter(r=>pos[r.source_id]&&pos[r.target_id]).map(r=>{const a=pos[r.source_id],b=pos[r.target_id];return `<g><line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="#afbed6" stroke-width="1.4" marker-end="url(#arrow)"/><text class="graph-edge-label" x="${(a.x+b.x)/2}" y="${(a.y+b.y)/2-5}" text-anchor="middle">${esc(relationDisplay(r))}</text></g>`}).join('');let nodes=es.map(e=>{let p=pos[e.id];return `<g class="graph-node" data-node="${esc(e.id)}"><circle cx="${p.x}" cy="${p.y}" r="22" fill="${e.kind==='class'?'#e5edff':'#e9f8f1'}" stroke="${e.kind==='class'?'#597bce':'#24a887'}" stroke-width="1.6"/><text text-anchor="middle" x="${p.x}" y="${p.y+36}" class="node-label">${esc(e.name.slice(0,12))}</text><circle cx="${p.x}" cy="${p.y}" r="5" fill="${e.kind==='class'?'#577ee2':'#27a387'}"/></g>`}).join('');return `<div class="panel"><h3>实体关系图 · 点击节点查看定义</h3><div class="graph-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="本体概念关系图"><defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="20" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#afbed6"/></marker></defs>${edges}${nodes}</svg></div><div class="smalltext muted" style="margin-top:7px">概念用蓝色表示，实例用绿色表示。仅预览已审核内容；候选不会出现在正式草稿。</div></div>`}
function renderReview(){const v=S.view,p=v.project;const pendingCount=Object.values(v.pending_totals||{}).reduce((n,x)=>n+x,0);const pending=v.proposals.filter(q=>q.status==='pending');const valid=v.validation;const versions=v.releases;return `<div class="subhead"><div><h2>05 / 审核、版本与回滚</h2><p>未确认的候选不得进入正式版本，发布前进行结构验证。</p></div></div>
<div class="stats"><div class="stat"><strong>${p.revision}</strong><small>当前草稿修订</small></div><div class="stat"><strong>v${p.published_version}</strong><small>已发布版本</small></div><div class="stat"><strong>${pendingCount}</strong><small>待审核候选</small></div><div class="stat"><strong>${valid.valid?'✓':'!'}</strong><small>结构校验</small></div></div>
${pendingCount?`<div class="notice warn">还有 ${pendingCount} 条近期待审候选（更多通过分页查看）。允许不采纳、不处理这些建议发布当前已确认本体；未决候选不会发布。</div>`:'<div class="notice ok">当前没有待审核候选。</div>'}
${valid.issues.length?`<div class="notice warn">${valid.issues.map(esc).join('<br>')}</div>`:''}
<div class="panel"><h3>当前草稿校验</h3><div class="kv-list"><div class="kv-row"><span>概念与实例</span><b>${valid.counts.entities}</b></div><div class="kv-row"><span>属性定义</span><b>${valid.counts.attributes}</b></div><div class="kv-row"><span>关系定义</span><b>${valid.counts.relations}</b></div><div class="kv-row"><span>引用与层级检查</span><b>${valid.valid?'通过':'未通过'}</b></div></div><div class="actionbar"><span class="hint">必须先进入“审核发布”阶段</span>${actionButton('✓ 发布正式版本','publish','primary',p.stage==='REVIEW'&&valid.valid?'':'disabled')}</div></div>
<div class="panel"><div class="subhead"><h3>与最近发布版本的差异</h3>${actionButton('↻ 查看变更','showDiff','neutral')}</div><div id="diffArea" class="smalltext muted">点击“查看变更”检查新增、删除或修改的对象。</div></div>
<div class="countline">历史正式版本 <span class="chip">${versions.length} 个</span></div><div class="releases">${versions.length?versions.map(r=>`<div class="release"><div><b>v${r.version}</b><span class="muted smalltext"> · 修订 ${r.revision}</span><div class="doc-info">${esc(r.note)} · ${esc(r.created_at)}</div></div><div class="toolbar">${actionButton('导出','releaseExport:'+r.version,'neutral')}${actionButton('恢复为草稿','restore:'+r.version,'neutral')}</div></div>`).join(''):'<div class="empty">尚未发布正式本体版本。</div>'}</div>
${p.stage==='RELATION'?`<div class="actionbar">${actionButton('进入审核发布 →','stage:REVIEW')}</div>`:p.stage==='REVIEW'?`<div class="actionbar">${actionButton('↶ 返回关系阶段','stage:RELATION','neutral')}</div>`:''}`}
function renderChat(){
 if(!S.view)return;
 $('#stageAssist').textContent='当前：'+STAGE_LABEL[STAGES.indexOf(S.view.project.stage)]+' · 实时进展同步';
 const box=$('#chatMessages');
 const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<90;
 const oldScroll=box.scrollTop;
 const events=(S.view.assistant_progress||[]).map(m=>({...m,_event:true}));
 const conversations=(S.view.messages||[]).map(m=>({...m,_event:false}));
 const items=[...events,...conversations].sort((a,b)=>a.created_at.localeCompare(b.created_at)||(a._event?1:-1)).slice(-170);
 let body=items.length?items.map(m=>{
  if(m._event){
    const ph=progressTitle(m.phase);
    const meta=[m.percent==null?'':m.percent+'%',m.eta_seconds==null?'':progressEta(m.eta_seconds)].filter(Boolean).join(' · ');
    return `<div class="bubble progress-event" data-progress-event="${esc(m.id)}"><div class="chat-event-label">✳ 解析进展 · ${esc(ph)}</div><div>${esc(m.content)}</div>${meta?`<div class="progress-event-meta">${esc(meta)}</div>`:''}</div>`;
  }
  return `<div class="bubble ${m.role==='user'?'user':''} ${m.source==='action'?'action-event':''}">${m.source==='action'?'<div class="chat-event-label">页面操作同步记录</div>':''}${esc(m.content)}</div>`;
 }).join(''):'<div class="bubble">你好！我是本体协商助手。导入材料并启动解析后，我会持续汇报正在阅读的内容、发现、进度和剩余时间估计。</div>';
 const jobs=S.view.jobs_latest||{};
 const live=[...(S.view.active_jobs||[]),jobs.review_all,jobs.parse,jobs.summary,jobs.ENTITY,jobs.PROPERTY,jobs.RELATION].find(j=>j&&['queued','running'].includes(j.state));
 if(live){
  const e=recentJobProgress(live);
  const pct=e?.percent;
  const started=Date.parse(live.updated_at||live.created_at);
  const idle=Number.isFinite(started)?Math.max(0,Math.floor((Date.now()-started)/1000)):0;
  const waiting=idle>=10?(live.job_kind==='review_all'?`逐条审核仍在执行，距上次进展 ${idle} 秒。`:`模型或解析程序仍在处理，距上次进展约 ${idle} 秒；长调用期间进度不会虚增。`):'';
  body+=`<div class="bubble progress-live" aria-live="polite"><div class="chat-event-label">◉ 正在执行 · ${esc(progressTitle(e?.phase||live.phase))}</div><div>${esc(e?.content||live.detail||'任务正在排队')}</div><div class="progress-live-head"><b>${pct==null?'准备中':pct+'%'}</b><span>${esc(live.job_kind==='review_all'?'逐条审核，失败继续':progressEta(e?.eta_seconds))}</span></div><div class="job-progress"><div style="width:${pct||0}%"></div></div>${waiting?`<div class="progress-event-meta">${esc(waiting)}</div>`:''}</div>`;
 }
 box.innerHTML=body;
 if(nearBottom)box.scrollTop=box.scrollHeight;
 else box.scrollTop=oldScroll;
 let stage=S.view.project.stage;
 let suggestions=stage==='TOPIC'?['这批资料的主要对象是什么？','建议排除哪些范围？']:stage==='ENTITY'?['这些概念是否存在重复？','从业务事实抽象自然人及业务角色']:stage==='PROPERTY'?['这些属性的类型合适吗？','哪些属性应该必填？']:stage==='RELATION'?['检查层级关系方向','是否存在循环依赖？']:['目前还缺哪些信息？'];
 const openGaps=(S.view.extraction_gaps||[]).filter(g=>g.stage===stage);
 if(openGaps.length){
   const reason=String(openGaps[0].reason||'未完成的模型提取').slice(0,65);
   suggestions=['请结合原文帮我处理当前抽取缺口：'+reason,...suggestions].slice(0,3);
 }
 $('#suggested').innerHTML=suggestions.map((x,i)=>`<button data-suggest="${i}">${esc(x)}</button>`).join('');
 document.querySelectorAll('[data-suggest]').forEach((el,i)=>el.onclick=()=>{$('#chatInput').value=suggestions[i];$('#chatInput').focus()});
}
function refreshBatchSelection(kind){
 const items=[...document.querySelectorAll(`[data-batch-id][data-batch-kind="${kind}"]`)];
 const selection=S.batchSelections[kind];
 for(const item of items)item.checked=selection.has(item.dataset.batchId);
 const master=document.querySelector(`[data-batch-select-all="${kind}"]`);
 if(master){master.checked=items.length>0&&items.every(item=>item.checked);master.indeterminate=items.some(item=>item.checked)&&!master.checked;}
 const counter=document.querySelector(`[data-batch-count="${kind}"]`);
 if(counter)counter.textContent=`已选择 ${selection.size} / ${items.length} 条`;
 document.querySelectorAll(`[data-batch-command="${kind}"]`).forEach(btn=>btn.disabled=selection.size===0);
}
function wireContent(){document.querySelectorAll('[data-action]').forEach(el=>el.onclick=()=>task(()=>action(el.dataset.action)));
 document.querySelectorAll('[data-batch-id]').forEach(el=>el.onchange=()=>{const set=S.batchSelections[el.dataset.batchKind];if(el.checked)set.add(el.dataset.batchId);else set.delete(el.dataset.batchId);refreshBatchSelection(el.dataset.batchKind)});
 document.querySelectorAll('[data-batch-select-all]').forEach(el=>{const kind=el.dataset.batchSelectAll;el.onchange=()=>{document.querySelectorAll(`[data-batch-id][data-batch-kind="${kind}"]`).forEach(item=>{if(el.checked)S.batchSelections[kind].add(item.dataset.batchId);else S.batchSelections[kind].delete(item.dataset.batchId)});refreshBatchSelection(kind)};refreshBatchSelection(kind)});let f=$('#fileInput');if(f)f.onchange=()=>task(()=>uploadFiles(f.files));let drop=$('#dropArea');if(drop){drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragging')};drop.ondragleave=()=>drop.classList.remove('dragging');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragging');task(()=>uploadFiles(e.dataTransfer.files))}}document.querySelectorAll('[data-node]').forEach(el=>el.onclick=()=>{let e=S.view.snapshot.entities.find(x=>x.id===el.dataset.node);if(e)showInfo(e.name,`类型：${e.kind}\n定义：${e.description||'暂无'}\nID：${e.id}`)})}
async function uploadFiles(files){
 if(!files||!files.length)return;
 const pid=S.id,failures=[];let succeeded=0,queued=0;
 for(const file of Array.from(files)){
  try{let body=new FormData();body.append('file',file);const r=await api(`/projects/${pid}/documents`,{method:'POST',body});succeeded++;if(r.queued)queued++;toast(`${r.queued?'已排队待解析':'已读取'}：${file.name}`)}
  catch(error){failures.push({name:file.name,error:error.message})}
 }
 if(S.id===pid){await loadProject(pid);S.tab='overview';renderContent()}
 if(failures.length)modal('文件导入结果 · 失败文件不阻断其他文件',`<p>成功接收 ${succeeded} 个（其中 ${queued} 个待后台解析）；失败 ${failures.length} 个。</p><div class="notice info">排队不等于解析成功；请查看右侧解析进度。网络响应异常时以工作空间文档列表为准，不自动重复上传。</div>${failures.map(x=>`<div class="notice warn"><b>${esc(x.name)}</b><p>${esc(x.error)}</p></div>`).join('')}`,[{text:'关闭',click:closeModal}]);
 else toast(`已接收 ${succeeded} 个文件${queued?'，后台解析 '+queued+' 个':''}`);
}

async function browseBaseline(kind){
 const r=await api(`/projects/${S.id}/openmind/baseline?kind=${kind}&limit=50`);
 const label=KINDS[kind]||kind;
 const rows=r.items.map(x=>`<div class="coverage-line"><div><b>${esc(x.name)}</b> · <span class="chip">${esc(x.code)}</span><div class="smalltext muted">UUID ${esc(x.id)}${x.dataType?' · '+esc(x.dataType):''}${x.executionMode?' · '+esc(x.executionMode):''}</div><div class="smalltext muted">${esc((x.description||x.steps||'').slice(0,180))}</div></div><button class="button neutral small" data-baselineid="${esc(x.id)}">原生详情</button></div>`).join('');
 modal(`OpenMind 分支已存在${label} · ${r.total} 项`,`<div class="notice info">这是指定分支的权威定义，仅读取、复用，不会在本地被改写。${r.total>r.items.length?'当前显示前 50 项，可通过 API 分页读取全部。':''}</div><div class="coverage-list">${rows||'暂无定义'}</div>`,[{text:'关闭',click:closeModal}]);
 document.querySelectorAll('[data-baselineid]').forEach(el=>el.onclick=()=>{
  const found=r.items.find(x=>x.id===el.dataset.baselineid);
  if(found)showInfo(`OpenMind ${label} · ${found.name}`,JSON.stringify(found,null,2));
 });
}
async function refreshBaseline(){let r=await api(`/projects/${S.id}/openmind/baseline/refresh`,{method:'POST',headers:omAdminHeaders()});await loadProject(S.id);toast(r.baseline.changed?'分支已有本体发生变化，旧待审候选已关闭，请重新核对':'已刷新 OpenMind 分支基线；现有内容未变化',7000)}
async function action(cmd){if(cmd==='formats')return showSupportedFormats();if(cmd==='modelingIntent')return showModelingIntent();if(cmd==='readingBoundaries')return showReadingBoundaries();if(cmd.startsWith('recheckIntent:'))return recheckIntent(cmd.split(':')[1]);if(cmd==='baselineRefresh')return refreshBaseline();if(cmd.startsWith('baselineList:'))return browseBaseline(cmd.split(':')[1]);if(cmd==='chooseFiles')return $('#fileInput').click();if(cmd==='analyze'){let r=await api(`/projects/${S.id}/jobs/start?kind=summary`,{method:'POST'});await loadProject(S.id);toast(r.already_running?'概要任务正在运行，已恢复进度显示':'概要生成任务已开始，进度会持续显示',6000);return}if(cmd==='reparse'){if(!confirm('从原始文件重新提取全部文本，然后重新确认概要主题？已审核本体及发布版本会保留。'))return;let r=await api(`/projects/${S.id}/documents/reparse`,{method:'POST'});await loadProject(S.id);toast(`已重新解析 ${r.reparsed} 份文件，请重新生成概要`,6500);return}if(cmd==='confirmTopic'){let summary=$('#summaryInput').value.trim(),scope=$('#scopeInput').value.trim();if(!summary||!scope){toast('请填写完整概要与建模范围');return}await api(`/projects/${S.id}/topic`,{method:'PUT',body:JSON.stringify({summary,scope})});await loadProject(S.id);S.tab='entity';renderTabs();renderContent();toast('主题已确认，开始实体构建');return}
 if(cmd.startsWith('page:')){let [,kind,delta]=cmd.split(':');return changeCandidatePage(kind,Number(delta))}if(cmd==='propose'){let r=await api(`/projects/${S.id}/jobs/start?kind=propose`,{method:'POST'});await loadProject(S.id);toast(r.already_running?'本阶段抽取正在运行，已恢复进度显示':'已开始全文自动抽取；逐批进度与校验缺口将持续更新',6000);return}if(cmd.startsWith('create:'))return showCandidateForm(cmd.split(':')[1]);if(cmd.startsWith('revise:')){let [,kind,id]=cmd.split(':');let a=S.view.snapshot[{entity:'entities',attribute:'attributes',relation:'relations'}[kind]].find(x=>x.id===id);return showCandidateForm(kind,null,a,'update')}if(cmd.startsWith('remove:')){let [,kind,id]=cmd.split(':');if(!confirm('删除操作仍需人工二次审批。是否创建删除候选？'))return;let payload=kind==='relation'?{operation:'delete',record_id:id}:{operation:'delete',target_id:id};let r=await api(`/projects/${S.id}/proposals`,{method:'POST',body:JSON.stringify({kind,payload,evidence:[]})});await loadProject(S.id);toast('已创建删除候选，请到审核区确认');return}if(cmd.startsWith('edit:')){let id=cmd.split(':')[1],q=(Object.values(S.pendingData).flatMap(x=>x?.items||[]).find(x=>x.id===id)||S.view.proposals.find(x=>x.id===id));if(!q){toast('候选列表已更新，请刷新本页后重试');return}return showCandidateForm(q.kind,q)}if(cmd.startsWith('allaccept:'))return approveAllPending(cmd.split(':')[1]);if(cmd.startsWith('reviewallreport:'))return showReviewAllReport(cmd.split(':')[1]);if(cmd.startsWith('bulk:')){let [,kind,decision]=cmd.split(':');return bulkDecide(kind,decision)}if(cmd.startsWith('approve:'))return decide(cmd.split(':')[1],'accept');if(cmd.startsWith('reject:')){let note=prompt('拒绝原因（可选）：');if(note===null)return;return decide(cmd.split(':')[1],'reject',null,note)}if(cmd.startsWith('stage:')){let target=cmd.split(':')[1];await api(`/projects/${S.id}/stage`,{method:'POST',body:JSON.stringify({target})});await loadProject(S.id);S.tab=({ENTITY:'entity',PROPERTY:'attribute',RELATION:'relation',REVIEW:'review'})[target]||'overview';renderTabs();renderContent();if(['entity','attribute','relation'].includes(S.tab))await fetchPendingPage(S.tab);return}if(cmd.startsWith('coverage:'))return showCoverage(cmd.split(':')[1]);if(cmd.startsWith('chunks:'))return showChunks(cmd.split(':')[1]);if(cmd==='publish')return showPublish();if(cmd==='showDiff')return showDiff();if(cmd.startsWith('restore:')){let version=cmd.split(':')[1];if(!confirm(`恢复 v${version} 将覆盖当前已确认草稿，并关闭所有待审核候选，是否继续？`))return;await api(`/projects/${S.id}/releases/${version}/restore`,{method:'POST'});await loadProject(S.id);toast('历史版本已恢复为新的草稿修订');return}if(cmd.startsWith('releaseExport:'))return download('ttl',Number(cmd.split(':')[1]));}
async function bulkDecide(kind,reviewAction){
 const ids=[...S.batchSelections[kind]];
 if(!ids.length){toast('请先勾选候选');return}
 if(ids.length>200){toast('一次最多审核 200 条，请缩小选择范围',6500);return}
 const proposed=(S.pendingData[kind]?.items||[]).filter(q=>ids.includes(q.id)&&q.kind===kind&&q.status==='pending');
 if(proposed.length!==ids.length){toast('候选列表已变化，请刷新后重新选择',6000);return}
 const choices=proposed.slice(0,5).map(q=>q.payload.name||q.payload.predicate||q.id).join('、');
 const suffix=proposed.length>5?' 等':'';
 const verb=reviewAction==='accept'?'同意':'拒绝';
 if(!confirm(`确定批量${verb} ${ids.length} 条${KINDS[kind]}候选？\n${choices}${suffix}\n\n逐条独立审核：通过的立即生效，失败的不变并列出原因。`))return;
 let note='';
 if(reviewAction==='reject'){note=prompt('批量拒绝原因（可选，对本次所选全部候选生效）：','');if(note===null)return}
 const result=await api(`/projects/${S.id}/proposals/batch-decision`,{method:'POST',body:JSON.stringify({kind,proposal_ids:ids,action:reviewAction,note,expected_revision:S.view.project.revision})});
 S.batchSelections[kind].clear();
 await loadProject(S.id);
 const failed=(result.results||[]).filter(x=>x.status==='failed');
 toast(`批量${verb}：成功 ${result.succeeded}，失败 ${failed.length}；失败候选仍待审核`,7500);
 if(failed.length)showInfo('批量审核失败项（其他项已成功）',failed.map(x=>x.id+'：'+x.reason).join('\n'));
 
}
function parseJobResult(job){try{return typeof job.result==='string'?JSON.parse(job.result):job.result||{}}catch{return {}}}
function isReviewAllRunning(kind){const j=S.view?.jobs_latest?.review_all;return j&&j.stage===KIND_TAB_STAGE(kind)&&['queued','running'].includes(j.state);}
async function approveAllPending(kind){
 const pid=S.id;
 const snap=await api(`/projects/${pid}/proposals/review-all/preview?kind=${encodeURIComponent(kind)}`);
 if(!snap.total){toast('当前没有待审候选');return;}
 const names=snap.examples.join('、');
 if(!confirm(`全部同意当前阶段的 ${snap.total} 条${KINDS[kind]}候选（跨所有页面）？\n例如：${names}\n新增 ${snap.operations?.add||0} · 修改 ${snap.operations?.update||0} · 删除 ${snap.operations?.delete||0}\n\n将逐条校验并写入本地草稿。失败项保持原状态，其余继续；不会自动发布或写入 OpenMind。确认后新增的候选不纳入本次。`))return;
 if(S.id!==pid)throw Error('工作空间已切换，请重新确认');
 const r=await api(`/projects/${pid}/proposals/review-all`,{method:'POST',body:JSON.stringify({kind,expected_revision:snap.revision,fingerprint:snap.fingerprint,confirmation:'CONFIRM_ACCEPT_ALL_PENDING'})});
 S.batchSelections[kind].clear();await loadProject(pid);
 toast(r.already_running?'已有跨页审核任务，正在显示进度':`已提交全部 ${snap.total} 条候选，后台逐条处理；可刷新后继续查看进度`,7000);
}
async function showReviewAllReport(jid){
 const data=await api(`/projects/${S.id}/jobs/${jid}`),job=data.job,report=parseJobResult(job);
 const html=`<div class="notice info">本次清单 ${report.total||job.total_units} 条；成功 ${report.succeeded||0}，失败 ${report.failed||0}，未处理 ${Math.max(0,(report.total||job.total_units)-(report.cursor||0))}。已成功项不会因其他失败回滚。</div>${report.stopped_reason?`<div class="notice warn">${esc(report.stopped_reason)}</div>`:''}<div style="max-height:55vh;overflow:auto">${(report.failures||[]).map(x=>`<div class="card"><strong>${esc(x.name||x.id)}</strong><p>${esc(x.reason)}</p><small>${esc(x.id)}</small></div>`).join('')||'<p>目前没有失败项。</p>'}</div>`;
 modal('跨页全部同意 · 审核结果',html,[{text:'关闭',click:closeModal},{text:'刷新结果',class:'neutral',click:()=>task(()=>showReviewAllReport(jid))}]);
}
async function decide(id,action,payload=null,note=''){let body={action,note,expected_revision:S.view.project.revision};if(payload)body.payload=payload;await api(`/projects/${S.id}/proposals/${id}/decision`,{method:'POST',body:JSON.stringify(body)});closeModal();await loadProject(S.id);toast(action==='accept'?'候选已通过审核并写入草稿':'候选已拒绝')}
function modal(title,body,actions=[]){$('#modalTitle').textContent=title;$('#modalBody').className='modal-body';$('#modalBody').innerHTML=body;$('#modalFoot').innerHTML='';actions.forEach(a=>{let btn=document.createElement('button');btn.className='button '+(a.class||'neutral');btn.textContent=a.text;btn.onclick=a.click;$('#modalFoot').append(btn)});$('#modal').classList.remove('hidden')}
function closeModal(){$('#modal').classList.add('hidden')}
function showInfo(title,details){modal(title,`<div style="white-space:pre-wrap;line-height:1.85">${esc(details)}</div>`,[{text:'关闭',click:closeModal}])}
function selectOptions(items,value){return items.map(x=>`<option value="${esc(x.id)}" ${value===x.id?'selected':''}>${esc(x.name)} · ${x.kind==='class'?'类':'实例'}</option>`).join('')}
function field(label,key,value='',type='text'){return `<div class="field"><label>${label}</label><input class="control" type="${type}" data-key="${key}" value="${esc(value)}"></div>`}
function selectField(label,key,choices,value){return `<div class="field"><label>${label}</label><select class="control" data-key="${key}">${choices.map(c=>`<option value="${esc(c[0])}" ${c[0]===value?'selected':''}>${esc(c[1])}</option>`).join('')}</select></div>`}
function showCandidateForm(kind,q=null,existing=null,operation='add'){
 let p=q?.payload||existing||{},es=S.view.snapshot.entities,inputs='';
 const title=kind==='entity'?'实体中文名称':kind==='attribute'?'属性中文名称':'关系中文名称';
 const label=kind==='relation'?relationDisplay(p):p.name;
 const currentLabel=label==='待补充中文名称'?'':label;
 const fields=field(title,'name',currentLabel)+field('英文 CODE（小写字母开头，仅 a-z、0-9、下划线）','code',p.code||'');
 if(kind==='entity')inputs=fields+field('定义与解释','description',p.description);
 if(kind==='attribute')inputs=`<div class="field"><label>所属实体</label><select class="control" data-key="owner_id">${selectOptions(es,p.owner_id)}</select></div>`+fields+selectField('数据类型','datatype',[['string','string / 字符串'],['integer','integer / 整数'],['double','double / 浮点数'],['boolean','boolean / 布尔'],['date','date / 日期']],p.datatype||'string')+`<div class="inline-options"><label><input type="checkbox" data-key="required" ${p.required?'checked':''}> 必填</label><label><input type="checkbox" data-key="multivalued" ${p.multivalued?'checked':''}> 允许多值（OpenMind 不支持）</label></div>`+field('描述','description',p.description);
 if(kind==='relation'){
  const semantic=[['','普通业务关系'],['subClassOf','类型继承（映射为父类）'],['hasPart','包含部件'],['partOf','属于整体']];
  if(p.predicate&&!semantic.some(v=>v[0]===p.predicate))semantic.push([p.predicate,'保留当前业务语义']);
  inputs=`<div class="field"><label>源实体</label><select class="control" data-key="source_id">${selectOptions(es,p.source_id)}</select></div>`+fields+`<div class="field"><label>目标实体</label><select class="control" data-key="target_id">${selectOptions(es,p.target_id)}</select></div>`+selectField('关系结构语义（独立于显示名称和 CODE）','predicate',semantic,p.predicate||'')+field('关系解释','description',p.description);
 }
 if(!inputs)return toast('候选类型无效');
 modal((q?'修改并接受':operation==='update'?'申请修改':'创建')+KINDS[kind]+'候选',`<p class="section-subtitle">中文名称用于展示；英文 CODE 是稳定标识。修改名称不会自动改变 CODE。未知名称请人工确认，不按编号猜测。</p><form id="candidateForm">${inputs}</form>`,[{text:'取消',click:closeModal},{text:q?'确认修改并接受':'添加待审候选',class:'primary',click:()=>task(async()=>{
   let out={};document.querySelectorAll('#candidateForm [data-key]').forEach(el=>out[el.dataset.key]=el.type==='checkbox'?el.checked:el.value);
   if(!/[\u3400-\u9fff]/.test(out.name||''))throw Error('请填写中文名称，不能使用英文 CODE 代替名称。');
   if(!/^[a-z][a-z0-9_]*$/.test(out.code||''))throw Error('请填写有效英文 CODE，例如 natural_person、created_at、owns_terminal。');
   if(out.code.length>(kind==='relation'?64:255))throw Error('CODE 过长。');
   if(kind==='relation'&&!out.predicate)out.predicate=out.code;
   if(q){await decide(q.id,'accept',out,'人工确认中文名称与英文 CODE 后接受')}
   else {if(operation==='update'){out.operation='update';if(kind==='relation')out.record_id=existing.id;else out.target_id=existing.id}
     let res=await api(`/projects/${S.id}/proposals`,{method:'POST',body:JSON.stringify({kind,payload:out,evidence:[]})});closeModal();await loadProject(S.id);toast(res.reused?.length?'OpenMind 已存在此定义，建议复用：'+res.reused[0]:'已添加待审核候选',6500)}
 })}]);
}

async function showCoverage(stage){
 const data=await api(`/projects/${S.id}/coverage?stage=${stage}`);
 const rows=data.batches.map(b=>`<div class="coverage-line"><div><b>第 ${b.batch}/${data.total} 批</b> · ${b.characters} 字符
 <span class="chip ${!b.processed||b.pending_created===0?'orange':'green'}">${!b.processed?'待扫描':`待审 ${b.pending_created} 条 · 复用 ${b.reused||0} 条 · 缺口 ${b.gaps} 条`}</span>
 <div class="smalltext muted">${esc(b.excerpt)}</div></div><button class="button neutral small" data-batch="${b.batch}">查看原文</button></div>`).join('');
 modal(`${stage} · 原文覆盖审计`,`<div class="notice info">${esc(data.notice)} · 已扫描 ${data.processed}/${data.total} 批。</div><div class="coverage-list">${rows}</div>`,[{text:'关闭',click:closeModal}]);
 document.querySelectorAll('[data-batch]').forEach(btn=>btn.onclick=()=>task(async()=>{
  const one=await api(`/projects/${S.id}/coverage?stage=${stage}&batch=${btn.dataset.batch}`);
  modal(`${stage} · 第 ${one.batch} 批原文`,`<div class="notice info">${esc(one.notice)}</div><pre class="coverage-pre">${esc(one.text)}</pre>`,[{text:'返回审计',click:()=>task(()=>showCoverage(stage))},{text:'关闭',click:closeModal}]);
 }));
}
async function showChunks(id){
 let data=await api(`/projects/${S.id}/documents/${id}/chunks`);
 const pageSize=100;
 function page(index){
  const pages=Math.max(1,Math.ceil(data.chunks.length/pageSize));
  const p=Math.max(0,Math.min(pages-1,index));
  const start=p*pageSize;
  let html=`<p class="section-subtitle">文件：${esc(data.document.filename)} · 共 ${data.chunks.length} 个原文区块 · 第 ${p+1}/${pages} 页。可翻页查看全部原文。</p>`;
  html+=`<div style="max-height:65vh;overflow-y:auto">${data.chunks.slice(start,start+pageSize).map(ch=>`<div class="evidence"><strong>${esc(ch.locator)}</strong><div style="white-space:pre-wrap">${esc(ch.text)}</div></div>`).join('')}</div>`;
  const actions=[{text:'关闭',click:closeModal}];
  if(p>0)actions.push({text:'上一页',click:()=>page(p-1)});
  if(p+1<pages)actions.push({text:'下一页',class:'primary',click:()=>page(p+1)});
  modal('原文证据浏览',html,actions);
 }
 page(0);
}
async function showDiff(){let d=await api(`/projects/${S.id}/diff`);let text=`对比发布版本 v${d.base_version} → 草稿修订 ${d.revision}：<br>`+Object.entries(d.changes).map(([kind,x])=>`${{entities:'实体',attributes:'属性',relations:'关系'}[kind]}：新增 ${x.added.length}、移除 ${x.removed.length}、修改 ${x.modified.length}`).join('<br>');$('#diffArea').innerHTML=text}
function showPublish(){modal('发布正式本体版本',`<p class="section-subtitle">将当前已审核草稿冻结成一个可导出的版本。历史版本不会被覆盖。</p>${field('版本备注','note','已完成一轮人机协商和审核')}`,[{text:'取消',click:closeModal},{text:'确认发布',class:'primary',click:()=>task(async()=>{let note=$('#modalBody [data-key="note"]').value;let r=await api(`/projects/${S.id}/publish`,{method:'POST',body:JSON.stringify({note})});closeModal();await loadProject(S.id);toast('正式版本 v'+r.published_version+' 已发布')})}])}
function download(format,version){let query=`format=${format}`+(version?`&version=${version}`:'');let url=`/api/projects/${S.id}/export?${query}`;task(async()=>{let res=await fetch(url,{headers:{...(S.token?{'X-API-Key':S.token}:{})}});if(!res.ok)throw new Error('导出失败：'+res.status);let blob=await res.blob();let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`ontology-${S.id}${version?'-v'+version:'-draft'}.${format}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000)})}
async function createProject(){
 const conf=await api('/settings/openmind');
 if(!conf.configured){
   modal('创建离线工作空间',`<div class="notice warn">OpenMind 尚未配置或认证凭据不完整。这将创建一个纯本地项目；如需继承 OpenMind 分支已有实体、属性和关系，请先点击顶部“OpenMind 对接”完成配置。</div><div class="field"><label>工作空间名称</label><input id="newProjectName" class="control" placeholder="例如：网关设备本体"></div>`,[
      {text:'取消',click:closeModal},{text:'创建本地项目',class:'primary',click:()=>task(async()=>{
       const name=$('#newProjectName').value.trim();if(!name)throw Error('请填写项目名称');
       const result=await api('/projects',{method:'POST',body:JSON.stringify({name})});closeModal();S.tab='overview';await reloadProjects();await loadProject(result.project.id);toast('本地项目已创建。')})}]);return;
 }
 modal('新建工作空间 · 选择 OpenMind 分支',`<div class="model-setting-note"><b>先选择目标分支，再创建工作空间</b><span>读取接入账号有权访问的领域和分支；创建前验证分支并加载已有实体/属性/关系。如发生返回异常，请以刷新后的工作空间列表为准，勿连续重复创建。</span></div>
 <div class="field"><label>工作空间名称</label><input id="newProjectName" class="control" placeholder="例如：网关设备结构本体"></div>
 <div class="field"><label>OpenMind 领域</label><select class="control" id="newDomain"><option value="">正在读取可见领域…</option></select></div>
 <div class="field"><label>开发分支</label><select class="control" id="newBranch" disabled><option value="">请先选择领域</option></select></div>
 <div id="createProjectState" class="notice info" role="status">正在从 OpenMind 读取有权限的领域与分支…</div>
 <div class="notice warn">当前后台使用一个 OpenMind 接入账号。分支列表代表该账号的读取权限；真正写回还需要该账号具备目标领域 admin 权限，并通过本地审批。</div>`,[
 {text:'取消',click:closeModal},{text:'创建并加载分支本体',class:'primary',click:async()=>{
   if(S.busy)return;const name=$('#newProjectName')?.value.trim(),domain_id=$('#newDomain')?.value,branch_id=$('#newBranch')?.value;
   if(!name||!domain_id||!branch_id){$('#createProjectState').textContent='必须填写名称并选择可用领域和 development 草稿分支。';return}
   S.busy=true;const info=$('#createProjectState');info.className='notice info';info.textContent='正在确认分支权限并读取既有实体、属性和关系。目录读取及校验成功后才会创建工作空间…';
   try{const result=await api('/projects',{method:'POST',headers:omAdminHeaders(),body:JSON.stringify({name,domain_id,branch_id})});closeModal();S.tab='overview';await reloadProjects();await loadProject(result.project.id);toast('工作空间已创建，已加载 OpenMind 分支基线：'+(result.openmind_baseline?.entity_count||0)+' 个实体。',8000)}
   catch(e){try{await reloadProjects()}catch(_){}info.className='notice warn';info.textContent='创建请求未能正常返回：'+e.message+'。工作空间列表已尝试刷新；请先检查是否实际创建成功，勿直接重复提交。'}finally{S.busy=false}
 }}]);
 try{
   const data=await api('/settings/openmind/domains',{headers:omAdminHeaders()});
   if($('#newDomain')===null)return;
   const domains=data.items||[];const domainEl=$('#newDomain'),branchEl=$('#newBranch'),state=$('#createProjectState');
   domainEl.innerHTML='<option value="">请选择领域</option>'+domains.map(x=>`<option value="${esc(x.id)}">${esc(x.name||x.code||x.id)} · ${esc(x.code||'')}</option>`).join('');
   domainEl.onchange=()=>{
     const chosen=domains.find(x=>x.id===domainEl.value),branches=chosen?.branches||[];
     branchEl.innerHTML='<option value="">请选择分支</option>'+branches.map(b=>`<option value="${esc(b.id)}" ${b.selectable?'':'disabled'}>${esc(b.version||b.id)} · ${esc(b.branchType)} / ${esc(b.status)} ${b.selectable?'':'（只读/不可编辑）'}</option>`).join('');
     branchEl.disabled=!branches.some(b=>b.selectable);
     state.className='notice info';state.textContent=branches.length?`该领域显示 ${branches.length} 个可读分支，其中 ${branches.filter(b=>b.selectable).length} 个可选草稿开发分支。`:'该领域没有可见分支。';
   };
   state.className='notice info';state.textContent=domains.length?`已读取 ${domains.length} 个可见领域。请选择领域和分支。`:'当前接入账号没有可见领域。请由 OpenMind 管理员分配权限。';
 }catch(e){const state=$('#createProjectState');if(state){state.className='notice warn';state.textContent='读取领域失败：'+e.message+'。请检查 OpenMind 账号权限与连接配置。'}}
}
async function sendChat(){let inp=$('#chatInput'),message=inp.value.trim();if(!message||!S.id)return;inp.value='';await task(async()=>{let result=await api(`/projects/${S.id}/chat`,{method:'POST',body:JSON.stringify({message})});await loadProject(S.id);toast(result.created?.length?`协商产生 ${result.created.length} 条待审核建议`:'已保存协商对话')})}
async function openModelSettings(){
 const config=await api('/settings/model');
 S.demoMode=Boolean(config.demo_mode);
 const source=config.source==='saved'?'页面已保存（持久化）':'环境变量（.env / LLM_*）';
 const html=`<div class="model-setting-note"><b>大模型连接设置</b><span>支持 OpenAI 兼容的 /chat/completions 接口；配置会保存到后端，不写入浏览器。</span></div>
 <div class="field"><label>API 基础地址（Base URL）</label><input class="control" id="modelBaseUrl" value="${esc(config.base_url)}" placeholder="https://api.example.com/v1" autocomplete="off"><div class="form-help">填写 /v1 结尾的基础地址，系统自动请求 /chat/completions。Docker 容器内的 127.0.0.1 指向容器自己。${config.demo_mode?' Demo 下直接填写 HTTP(S) 地址，无需 MODEL_ALLOWED_HOSTS。':''}</div></div>
 <div class="field"><label>模型 ID</label><input class="control" id="modelId" value="${esc(config.model)}" placeholder="qwen3-32b 或你部署的模型 ID" autocomplete="off"></div>
 <div class="field"><label>模型供应商 API Key ${config.has_api_key?'<span class="chip green">已配置</span>':'<span class="chip">未配置，可选</span>'}</label><input id="modelApiKey" class="control" type="password" autocomplete="new-password" placeholder="${config.has_api_key?'留空沿用现有密钥（地址变更除外）':'若服务不要求鉴权，可以留空'}"><label class="checkbox-line"><input id="modelClearApiKey" type="checkbox"> 清除已保存的 API Key</label></div>
 ${S.demoMode?'':`<div class="field"><label>模型管理员密钥（仅设置 MODEL_ADMIN_TOKEN 时需要）</label><input id="modelAdminToken" class="control" type="password" autocomplete="off" placeholder="模型配置管理员 Token" value="${esc(S.modelAdminToken)}"></div>`}
 <div class="settings-status"><span>来源：${source}</span><span>当前运行模式：<b>${config.mode==='LLM'?'真实模型':'规则演示'}</b></span>${config.updated_at?`<span>保存于：${esc(config.updated_at)}</span>`:''}</div>
 <div id="modelTestStatus" class="notice info">先保存配置，再点击“测试连接”。测试请求会由后端发往你的模型服务。</div>
 ${S.demoMode?`<div class="notice warn">Demo 模式：无需 OntoFlow 管理密钥。模型供应商 API Key（若需要）将以可逆明文形式保存在本地数据库，不要用于公网或真实敏感凭据。${config.credential_reset_required?'旧版加密的 API Key 无法用当前密钥读取，请重新填写模型供应商 API Key 并保存。':''}</div>`:'<div class="notice warn">安全提示：API Key 仅在后端加密存储，不会返回给浏览器。修改服务地址且不重新填写 Key 时，会清除旧 Key，避免发送给新地址。</div>'}`;
 modal('模型配置 · 对话协商引擎',html,[
   {text:'关闭',click:closeModal},
   {text:'恢复 .env',click:()=>task(async()=>{if(!confirm('删除页面保存的模型配置并恢复环境变量？'))return;S.modelAdminToken=$('#modelAdminToken')?.value||'';await api('/settings/model',{method:'DELETE',headers:modelAdminHeaders()});await refresh();await openModelSettings();toast('已恢复 .env 配置')})},
   {text:'测试连接',click:()=>task(async()=>{S.modelAdminToken=$('#modelAdminToken')?.value||'';const el=$('#modelTestStatus');el.className='notice info';el.textContent='正在测试后端到模型服务的连接…';try{const result=await api('/settings/model/test',{method:'POST',headers:modelAdminHeaders()});el.className='notice ok';el.textContent='连接成功 · '+result.model+' · 回复：'+result.reply_preview;}catch(e){el.className='notice warn';el.textContent='连接失败：'+e.message}})},
   {text:'保存配置',class:'primary',click:()=>task(async()=>{S.modelAdminToken=$('#modelAdminToken')?.value||'';const body={base_url:$('#modelBaseUrl').value.trim(),model:$('#modelId').value.trim(),api_key:$('#modelApiKey').value,clear_api_key:$('#modelClearApiKey').checked};await api('/settings/model',{method:'PUT',headers:modelAdminHeaders(),body:JSON.stringify(body)});await refresh();await openModelSettings();toast('模型配置已保存并生效，无需重启')})}
 ]);
}
function modelAdminHeaders(){return S.modelAdminToken?{'X-Model-Admin-Key':S.modelAdminToken}:{};}
$('#modelSettingBtn').onclick=()=>task(openModelSettings);

function openMindHostSettings(config){
 if(config.demo_mode)return '<div class="notice info" data-connection-policy="demo_unrestricted">Demo 连接模式：直接填写 OpenMind 服务地址即可。支持 HTTP / HTTPS，不要求页面白名单、部署白名单或内网 HTTP 开关；旧的白名单配置不参与校验。仅用于可信本机或隔离演示网络。</div>';
 return ` <div class="field"><label>允许访问的主机名（白名单，逗号或换行分隔）</label><textarea id="omAllowedHosts" rows="2" class="control" placeholder="openmind.example.com\n10.0.0.12">${esc(config.allowed_hosts.join('\n'))}</textarea><div class="form-help">必须包含 Base URL 的主机名，不填写协议、端口或通配符。生产环境默认要求 HTTPS；仅部署管理员在 .env 启用可信内网 HTTP 且配置部署层白名单后，才能使用 HTTP。</div></div>
 ${config.http_compat_enabled?'<div class="notice warn">当前已启用内网 HTTP 兼容开关。仅允许部署白名单内的私有地址；登录密码和令牌经 HTTP 传输，务必使用可信隔离网络。</div>':'<div class="form-help">生产默认禁止 HTTP；如确需内网 HTTP，部署管理员需修改 .env 并重启服务，不能仅在页面开启。</div>'}`;
}

async function openOpenMind(){
 const config=await api('/settings/openmind');
 S.demoMode=Boolean(config.demo_mode);
 const preferLogin=config.auth_mode==='login'||(!config.configured&&!config.has_access_token);
 const status=S.id?await api(`/projects/${S.id}/openmind/status`):null;
 const binding=status?.branch||{};
 const html=`<div class="model-setting-note"><b>OpenMind 后台自动认证</b><span>保存服务地址与专用账号后，后端自动登录、刷新令牌并获取可访问领域。创建工作空间时选择分支；无需人工复制访问 Token。</span></div>
 <div class="field"><label>OpenMind API Base URL</label><input class="control" id="omBaseUrl" value="${esc(config.base_url)}" placeholder="https://openmind.example.com" autocomplete="off"><div class="form-help">只填写服务根地址，不加 /api/v1。Docker 内 localhost/127.0.0.1 指向容器本身；若 OpenMind 运行在 Mac 上，请用 http://host.docker.internal:实际端口。</div></div>
 <div class="field"><label>认证方式</label><select class="control" id="omAuthMode"><option value="login" ${preferLogin?'selected':''}>后台账号登录 · 自动维护 Token（推荐）</option><option value="token" ${!preferLogin?'selected':''}>手动 Bearer Token（兼容旧版）</option></select></div>
 <div class="field om-login-fields"><label>OpenMind 接入账号</label><input id="omUsername" class="control" value="${esc(config.username||'')}" autocomplete="username" placeholder="预先分配了领域权限的 OpenMind 用户名"></div>
 <div class="field om-login-fields"><label>OpenMind 登录密码 ${config.has_password?'<span class="chip green">已保存</span>':'<span class="chip orange">未配置</span>'}</label><input id="omPassword" type="password" class="control" autocomplete="new-password" placeholder="${config.has_password?'留空保留原有密码（切换服务或账号除外）':'填写接入账号密码'}"><label class="checkbox-line"><input type="checkbox" id="omClearPassword"> 清除已保存的密码</label></div>
 <div class="field om-token-fields"><label>OpenMind 用户访问 Token ${config.has_access_token?'<span class="chip green">已保存</span>':'<span class="chip orange">未配置</span>'}</label><input id="omAccessToken" type="password" class="control" autocomplete="new-password" placeholder="${config.has_access_token?'留空保留原有 Token（改变地址除外）':'具有目标 development 分支写权限的 Bearer Token'}"><label class="checkbox-line"><input type="checkbox" id="omClearAccess"> 清除已保存的访问 Token</label></div>
 ${openMindHostSettings(config)}
 ${S.demoMode?'':`<div class="field"><label>现有 OpenMind 管理密钥 ${config.has_admin_token?'<span class="chip green">已设置</span>':'<span class="chip">未设置</span>'}</label><input id="omAdmin" class="control" type="password" autocomplete="off" value="${esc(S.openmindAdminToken)}" placeholder="页面保存/同步需要的当前管理密钥"></div>
 <div class="field"><label>模型管理员密钥（仅首次初始化或管理员恢复时需要）</label><input id="omModelAdmin" class="control" type="password" autocomplete="off" value="${esc(S.modelAdminToken)}" placeholder="可使用原模型管理页面的管理员密钥"></div>
 <div class="field"><label>设置或轮换 OpenMind 管理密钥（至少 12 字符）</label><input id="omNewAdmin" class="control" type="password" autocomplete="new-password" placeholder="留空则保持不变"><label class="checkbox-line"><input id="omClearAdmin" type="checkbox"> 移除已保存的管理密钥（如存在环境变量密钥则回退到环境变量）</label></div>
 `}
 <div class="settings-status"><span>配置来源：${config.source==='saved'?'页面持久化':'兼容的 .env 默认配置'}</span><span>状态：${config.configured?'已填写连接信息':'尚未完成配置'}</span>${config.updated_at?`<span>更新时间：${esc(config.updated_at)}</span>`:''}</div>
 <div class="settings-actions"><button id="omSaveConfig" class="button primary small">保存全局连接设置</button><button id="omTestConfig" class="button neutral small">测试服务连接</button><button id="omResetConfig" class="button neutral small">恢复环境默认</button></div>
 <div id="omConfigResult" class="notice info">先保存，再测试。只读测试不会写入 OpenMind。遇到 ConnectError 先检查容器网络/地址；只有写入响应中断时才需要回读 Catalog。</div>
 ${S.id?`<div class="om-section-title">当前项目 · 领域/分支绑定</div>
 <div class="field"><label>根领域 UUID（domainId）</label><input id="omDomain" class="control" value="${esc(binding.domain_id||'')}" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"></div>
 <div class="field"><label>可写 development 分支 UUID（branchId）</label><input id="omBranch" class="control" value="${esc(binding.branch_id||'')}" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"></div>
 <div class="settings-actions"><button id="omSaveBinding" class="button neutral small">保存项目绑定</button><button id="omTestBranch" class="button neutral small">测试当前分支</button><button id="omCode" class="button neutral small">调整对象 CODE</button><button id="omPreview" class="button neutral small">预览写入计划</button><button id="omSync" class="button primary small">审核后写入 OpenMind</button><button id="omConflicts" class="button neutral small">写入冲突与恢复</button></div>
 <div class="settings-status"><span>已同步映射：${status.mappings.length} 项</span><span>目标：${esc(status.endpoint||'未设置')}</span></div>
 <div class="notice ${status.baseline?'ok':'warn'}">${status.baseline?`已加载分支基线：实体 ${status.baseline.entity_count} · 属性 ${status.baseline.attribute_count} · 关系 ${status.baseline.relation_count} · 最近同步 ${esc(status.baseline.refreshed_at)}`:'分支基线尚未加载；任何文档建模之前都必须先读取现有目录。'}</div>
 <div class="settings-actions"><button id="omFetchBaseline" class="button neutral small">↻ 刷新现有分支本体</button><button id="omBrowseEntities" class="button neutral small">查看既有实体</button><button id="omBrowseAttributes" class="button neutral small">查看既有属性</button><button id="omBrowseRelations" class="button neutral small">查看既有关系</button></div>
 <div id="omResult" class="notice info" style="max-height:28vh;overflow:auto;white-space:pre-wrap">先绑定开发分支并预览本体写入计划。保存定义不等于资源绑定或上线。</div>`:'<div class="notice info">完成后台认证配置后，点击“创建工作空间”，直接从有权限的领域与分支中选择目标并加载本体。</div>'}
 ${S.demoMode?`<div class="notice warn">Demo 模式：不要求 OntoFlow API Key/管理员密钥，OpenMind 账号密码在本地 PG 中使用可逆明文存储；OpenMind 远端登录认证仍然有效。${config.credential_reset_required?'旧版加密凭据无法读取，请重新填写 OpenMind 密码并保存。':''}</div>`:'<div class="notice warn">安全要求：令牌在后端加密保存，管理密钥仅保存加盐哈希；密钥不会回显。生产首次初始化需要已有的模型管理员密钥（服务端引导权限），不能通过未授权页面自行提升管理员权限。</div>'}`;
 modal('OpenMind · 服务配置及项目对接',html,[{text:'关闭',click:closeModal}]);
 const readAdmin=()=>{S.openmindAdminToken=$('#omAdmin')?.value||'';S.modelAdminToken=$('#omModelAdmin')?.value||''};
 const toggleAuth=()=>{const login=$('#omAuthMode').value==='login';document.querySelectorAll('.om-login-fields').forEach(el=>el.classList.toggle('hidden',!login));document.querySelectorAll('.om-token-fields').forEach(el=>el.classList.toggle('hidden',login))};$('#omAuthMode').onchange=toggleAuth;toggleAuth();
 const show=(selector,value,ok)=>{const el=$(selector);el.className='notice '+(ok?'ok':'warn');el.textContent=value};
 $('#omSaveConfig').onclick=()=>task(async()=>{
    readAdmin(); const body={base_url:$('#omBaseUrl').value.trim(),auth_mode:$('#omAuthMode').value,username:$('#omUsername').value.trim(),password:$('#omPassword').value,clear_password:$('#omClearPassword').checked,access_token:$('#omAccessToken').value,
     clear_access_token:$('#omClearAccess').checked,new_admin_token:$('#omNewAdmin')?.value||'',
     clear_admin_token:$('#omClearAdmin')?.checked||false};
    if(!S.demoMode)body.allowed_hosts=($('#omAllowedHosts')?.value||'').split(/[,\n]/).map(s=>s.trim()).filter(Boolean);
    await api('/settings/openmind',{method:'PUT',headers:omAdminHeaders(),body:JSON.stringify(body)});
    if(body.new_admin_token)S.openmindAdminToken=body.new_admin_token;
    toast(S.demoMode?'OpenMind 服务配置已保存并生效':'OpenMind 服务配置已加密保存并生效');await openOpenMind();
 });
 $('#omTestConfig').onclick=()=>task(async()=>{readAdmin();try{
   const result=await api('/settings/openmind/test',{method:'POST',headers:omAdminHeaders()});
   show('#omConfigResult','认证和 API 连接成功：'+JSON.stringify(result),true);
 }catch(e){show('#omConfigResult','连接失败：'+e.message,false)}});
 $('#omResetConfig').onclick=()=>task(async()=>{readAdmin();if(!confirm('删除页面保存的 OpenMind 服务配置及本地管理密钥？项目绑定和同步记录保留。'))return;
   await api('/settings/openmind',{method:'DELETE',headers:omAdminHeaders()});S.openmindAdminToken='';toast('已恢复环境默认设置');await openOpenMind()});
 if(!S.id)return;
 $('#omSaveBinding').onclick=()=>task(async()=>{readAdmin();const r=await api(`/projects/${S.id}/openmind/binding`,{method:'PUT',headers:omAdminHeaders(),body:JSON.stringify({domain_id:$('#omDomain').value.trim(),branch_id:$('#omBranch').value.trim()})});await loadProject(S.id);toast(`绑定成功，已读取 ${r.baseline?.entity_count||0} 个既有实体、${r.baseline?.attribute_count||0} 个属性、${r.baseline?.relation_count||0} 个关系`,7000);await openOpenMind()});
 $('#omTestBranch').onclick=()=>task(async()=>{readAdmin();try{let r=await api(`/projects/${S.id}/openmind/test`,{method:'POST',headers:omAdminHeaders()});show('#omResult','连接成功：'+JSON.stringify(r),true)}catch(e){show('#omResult','当前分支连接失败：'+e.message,false)}});
 $('#omConflicts').onclick=()=>task(async()=>{readAdmin();await showSyncConflicts()});
 $('#omFetchBaseline').onclick=()=>task(async()=>{readAdmin();await refreshBaseline();await openOpenMind()});
 for(const [id,kind] of [['omBrowseEntities','entity'],['omBrowseAttributes','attribute'],['omBrowseRelations','relation']])$('#'+id).onclick=()=>task(()=>browseBaseline(kind));
 $('#omCode').onclick=()=>task(async()=>{readAdmin();const plan=await api(`/projects/${S.id}/openmind/plan`);const list=plan.steps.map((s,i)=>`${i+1}. ${s.kind} / ${s.data.name} => ${s.data.code}`).join('\n');const index=Number(prompt('输入修改序号：\n'+list));const entry=plan.steps[index-1];if(!entry)return;const code=prompt('新 CODE：',entry.data.code);if(!code)return;await api(`/projects/${S.id}/openmind/code`,{method:'PUT',headers:omAdminHeaders(),body:JSON.stringify({kind:entry.kind,local_id:entry.local_id,code})});toast('英文 CODE 已保存，需重新发布本地版本后再预览同步')});
 $('#omPreview').onclick=()=>task(async()=>{let p=await api(`/projects/${S.id}/openmind/plan`);$('#omResult').textContent=JSON.stringify({ready:p.ready,blockers:p.blockers,warnings:p.warnings,counts:p.counts,steps:p.steps},null,2);if(p.ready)toast('本地校验通过；提交前将重新读取 Catalog')});
 $('#omSync').onclick=()=>task(async()=>{readAdmin();const plan=await api(`/projects/${S.id}/openmind/plan?remote=true`);if(!plan.ready){$('#omResult').textContent=JSON.stringify({blockers:plan.blockers,warnings:plan.warnings},null,2);throw Error('请先处理不兼容项')}
   const confirmText=prompt(`本次向分支 ${plan.branchId} 执行 ${plan.steps.length} 次独立写操作，非批量原子事务。请输入 CONFIRM_OPENMIND_WRITE：`);if(confirmText!=='CONFIRM_OPENMIND_WRITE')return;
   const result=await api(`/projects/${S.id}/openmind/sync`,{method:'POST',headers:omAdminHeaders(),body:JSON.stringify({expected_revision:plan.project_revision,confirmation:confirmText})});$('#omResult').textContent=JSON.stringify(result,null,2);toast('写入及目录回读完成；不代表上线')});
}
function omAdminHeaders(){return {...(S.openmindAdminToken?{'X-OpenMind-Admin-Token':S.openmindAdminToken}:{}),...modelAdminHeaders()}}
$('#openmindBtn').onclick=()=>task(openOpenMind);
$('#modelSettingTopBtn').onclick=()=>task(openModelSettings);
$('#newProject').onclick=()=>task(createProject);$('#heroCreate').onclick=()=>task(createProject);$('#refreshBtn').onclick=()=>task(refresh);$('#settingBtn').onclick=()=>modal('API 访问设置',`<div class="notice warn">生产使用必须在可信的服务端网关认证，并转发已验证身份；浏览器存储 Token 仅用于本地调试。</div><div class="field"><label>X-API-Key（保存在浏览器当前标签页的 sessionStorage）</label><input id="tokenInput" class="control" type="password" value="${esc(S.token)}" /></div>`,[{text:'取消',click:closeModal},{text:'应用',class:'primary',click:()=>{S.token=$('#tokenInput').value;sessionStorage.setItem('ontologyToken',S.token);closeModal();task(refresh)}}]);$('#closeModal').onclick=closeModal;$('#modal').onclick=e=>{if(e.target.id==='modal')closeModal()};$('#sendChat').onclick=sendChat;$('#chatInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}};$('#exportJson').onclick=()=>download('json');$('#exportTtl').onclick=()=>download('ttl');$('#exportOwl').onclick=()=>download('owl');$('#exportJsonld').onclick=()=>download('jsonld');
task(async()=>{try{const health=await api('/health');S.demoMode=Boolean(health.demo_mode);if(S.demoMode){$('#settingBtn')?.classList.add('hidden');const banner=$('.local-pill');if(banner)banner.textContent='DEMO · 无内部密钥 / 主机白名单';S.token='';S.modelAdminToken='';S.openmindAdminToken='';sessionStorage.removeItem('ontologyToken');}}catch(e){console.warn('无法读取 Demo 模式状态',e)}await refresh()});

async function showGapManager(status='open',offset=0){
 if(!S.id)return;
 const pid=S.id;
 const data=await api(`/projects/${pid}/gaps?status=${encodeURIComponent(status)}&offset=${offset}&limit=20`);
 const labels={open:'待处理',deferred:'暂缓',resolved:'已处置',retrying:'重试中'};
 const html=`<div class="notice info">处理决定会同步到右侧协商对话。记录已处置不会自动修改正式本体；定向重试只处理该来源单元。</div>
 <label>问题状态 <select id="gapStatus" class="control">${['open','retrying','deferred','resolved','all'].map(x=>`<option value="${x}" ${x===status?'selected':''}>${labels[x]||'全部'}</option>`).join('')}</select></label>
 <div style="max-height:55vh;overflow:auto">${data.items.map(g=>`<section class="card" style="margin-top:8px;padding:12px">
 <strong>${esc(labels[g.status]||g.status)} · ${esc(g.stage)} · 来源单元 ${g.batch_number}</strong><p>${esc(g.reason)}</p>
 <small>${esc(g.payload?.source_excerpt||g.payload?.locator||'')}</small>
 ${g.decision?`<p>处置说明：${esc(g.decision)}</p>`:''}
 <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${(g.status==='retrying'?[]:g.status==='resolved'||g.status==='deferred'?['reopen']:['retry','defer','resolve']).map(action=>`<button class="button neutral small" data-gap="${esc(g.id)}" data-gap-action="${action}" data-gap-version="${g.decision_revision}">${({retry:'只重试此来源',defer:'暂缓',resolve:'记录处置结论',reopen:'重新打开'})[action]}</button>`).join('')}</div></section>`).join('')||'<p>当前状态没有问题。</p>'}</div>
 <div class="smalltext">共 ${data.total} 项；当前 ${offset+1}–${Math.min(offset+20,data.total)}</div>
 <button id="gapPrev" class="button neutral small" ${offset===0?'disabled':''}>上一页</button><button id="gapNext" class="button neutral small" ${offset+20>=data.total?'disabled':''}>下一页</button>`;
 modal('抽取缺口与建模决定',html,[{text:'关闭',click:closeModal}]);
 $('#gapStatus').onchange=e=>task(()=>showGapManager(e.target.value,0));
 $('#gapPrev').onclick=()=>task(()=>showGapManager(status,Math.max(0,offset-20)));
 $('#gapNext').onclick=()=>task(()=>showGapManager(status,offset+20));
 document.querySelectorAll('[data-gap-action]').forEach(el=>el.onclick=()=>task(async()=>{
   const note=window.prompt('请输入本次处置理由（不会自动批准或写入本体）：');
   if(!note?.trim())return;
   const response=await api(`/projects/${pid}/gaps/${el.dataset.gap}/decision`,{method:'POST',body:JSON.stringify({
     action:el.dataset.gapAction,note:note.trim(),expected_version:Number(el.dataset.gapVersion)})});
   await loadProject(pid);await showGapManager(status,offset);
   toast(response.job_id?'已提交单个来源单元重试，其余结果不受影响。':'决定已保存并同步到协商记录。');
 }));
}

$("#gapManagerBtn").onclick=()=>task(()=>showGapManager());

async function showSyncConflicts(){
 if(!S.id)return;
 const pid=S.id;
 const data=await api(`/projects/${pid}/openmind/conflicts/check`,{method:'POST',headers:omAdminHeaders()});
 const display=value=>typeof value==='string'?value:JSON.stringify(value,null,2)??'（字段不存在）';
 const html=`<div class="notice warn">本页只回读 OpenMind，不发送写入。相同的已保存结果会恢复映射；真实差异不会自动覆盖。剩余 ${data.remaining} 项，本次确认 ${data.reconciled} 项。</div>
 <div style="max-height:62vh;overflow:auto">${data.items.map(op=>`<section class="card" style="padding:14px;margin:10px 0"><strong>${esc(KINDS[op.kind]||op.kind)} · ${esc(op.name)}</strong><p>CODE：${esc(op.code)} · 状态：${esc(op.state)}</p><small style="overflow-wrap:anywhere">操作编号：${esc(op.id)}<br>对象 UUID：${esc(op.remote_id)}</small><div class="table-scroll"><table class="data-table"><thead><tr><th>差异字段</th><th>旧审批期望</th><th>当前远端</th></tr></thead><tbody>${op.differences.map(d=>`<tr><td>${esc(d.field)}</td><td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-width:300px">${esc(display(d.expected))}</pre></td><td><pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-width:300px">${esc(display(d.actual))}</pre></td></tr>`).join('')}</tbody></table></div><p>${esc(op.hint)}</p>${op.can_return_to_review?`<button class="button neutral small" data-return-review="${esc(op.id)}">保留当前远端，撤销旧授权并重新审核</button>`:''}</section>`).join('')||'<p>没有待处理冲突。可刷新基线并重新预览，不需要删除数据库记录。</p>'}</div>`;
 modal('OpenMind · 写入冲突与恢复',html,[{text:'关闭',click:closeModal},{text:'再次回读核对',class:'neutral',click:()=>task(showSyncConflicts)}]);
 document.querySelectorAll('[data-return-review]').forEach(button=>button.onclick=()=>task(async()=>{
  if(S.id!==pid)throw Error('工作空间已变化');
  const op=data.items.find(x=>x.id===button.dataset.returnReview);
  if(!op)return;
  if(!confirm(`保留 OpenMind 当前的「${op.name}」，撤销本次旧写入授权？\n\n不会覆盖远端，也不会自动改写本地草稿。之后必须处理其余冲突、刷新基线、重新审核发布并预览，才能再次同步。`))return;
  const note=prompt('请填写核对意见（必填）：','');if(note===null)return;if(!note.trim())throw Error('请填写实际核对意见');
  const r=await api(`/projects/${pid}/openmind/operations/${op.id}/return-to-review`,{method:'POST',headers:omAdminHeaders(),body:JSON.stringify({expected_revision:data.revision,observation_hash:op.observation_hash,confirmation:'KEEP_REMOTE_AND_REVIEW_AGAIN',note})});
  await loadProject(pid);await showSyncConflicts();toast(r.message,10000);
 }));
}

if($('#modelingIntentBtn'))$('#modelingIntentBtn').onclick=()=>task(showModelingIntent);
