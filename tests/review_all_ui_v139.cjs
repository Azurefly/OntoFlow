'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const src=fs.readFileSync(path.join(__dirname,'..','static','app.js'),'utf8')
 .split('\n').filter(line=>!line.startsWith("task(async()=>{try{const health=")).join('\n');
const node={classList:{add(){},remove(){},toggle(){}},querySelectorAll(){return []},style:{},onclick:null};
const context={sessionStorage:{getItem(){return ''},setItem(){},removeItem(){}},
 document:{querySelector(){return node},querySelectorAll(){return []},body:{style:{}}},
 setTimeout(){return 1},clearTimeout(){},console,window:{},FormData:class{},
 fetch(){throw Error('network not allowed')},confirm(){return true},prompt(){return '人工核对'}};
vm.createContext(context);vm.runInContext(src,context);
const run=js=>vm.runInContext(js,context);
run(`
 S.id='workspace';S.tab='attribute';
 S.view={project:{id:'workspace',scope_confirmed:true,stage:'PROPERTY',revision:20},
 snapshot:{entities:[{id:'owner',name:'设备',kind:'class',description:''}],attributes:[],relations:[]},
 active_jobs:[],jobs_latest:{},extraction_gaps:[],pending_totals:{attribute:318}};
 S.pendingData.attribute={total:318,items:Array.from({length:20},(_,i)=>({id:'p'+i,kind:'attribute',status:'pending',stage:'PROPERTY',payload:{name:'字段'+i,owner_id:'owner'},evidence:[]}))};
`);
let html=run(`renderModelTab('attribute')`);
assert.ok(html.includes('全部同意（跨页 318 条）'));
assert.ok(html.includes('全选本页'));
assert.ok(html.includes('人工审核候选共 318 条 · 当前页 20 条'));
run(`S.view.jobs_latest.review_all={id:'job1',stage:'PROPERTY',job_kind:'review_all',state:'running',total_units:318,completed_units:40,result:JSON.stringify({succeeded:39,failed:1}),detail:'审核中'};`);
html=run(`renderModelTab('attribute')`);
assert.match(html,/data-action="allaccept:attribute" disabled/);
assert.ok(html.includes('40/318'));
let calls=[];context.calls=calls;
run(`S.view.jobs_latest={};api=async function(url,opts={}){calls.push({url,body:opts.body?JSON.parse(opts.body):null});
 if(url.includes('preview'))return {kind:'attribute',revision:20,total:318,fingerprint:'a'.repeat(64),examples:['字段1','字段2'],operations:{add:318}};
 return {job:{id:'job2'},already_running:false};};loadProject=async()=>{};toast=()=>{};`);
(async()=>{
 await run(`approveAllPending('attribute')`);
 assert.equal(calls.length,2);
 assert.ok(calls[0].url.endsWith('preview?kind=attribute'));
 assert.ok(calls[1].url.endsWith('/proposals/review-all'));
 assert.equal(calls[1].body.confirmation,'CONFIRM_ACCEPT_ALL_PENDING');
 assert.equal(calls[1].body.fingerprint,'a'.repeat(64));
 assert.ok(!('proposal_ids' in calls[1].body)); // never merely the current 20 IDs
 run(`api=async()=>({remaining:1,reconciled:0,revision:20,items:[{id:'op1',name:'设备',code:'device',kind:'entity',remote_id:'uuid1',state:'CONFLICT',hint:'请核对',can_return_to_review:true,differences:[{field:'description',expected:'本地',actual:'<script>unsafe</script>'}]}]});modal=(title,body,actions)=>{globalThis.modalResult={title,body,actions};};`);
 await run('showSyncConflicts()');
 const modal=run('modalResult');
 assert.equal(modal.title,'OpenMind · 写入冲突与恢复');
 assert.ok(modal.body.includes('description'));
 assert.ok(modal.body.includes('&lt;script&gt;'));
 assert.ok(!modal.body.includes('<script>unsafe'));
 assert.ok(modal.body.includes('保留当前远端，撤销旧授权并重新审核'));
 console.log('UI cross-page 318 count, frozen confirmation, running state, safe conflict rendering: PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
