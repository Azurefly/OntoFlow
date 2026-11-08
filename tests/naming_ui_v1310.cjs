'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const src=fs.readFileSync(path.join(__dirname,'..','static','app.js'),'utf8')
 .split('\n').filter(line=>!line.startsWith('task(async()=>{try{const health=')).join('\n');
const node={classList:{add(){},remove(){},toggle(){}},querySelectorAll(){return []},style:{},onclick:null};
const context={sessionStorage:{getItem(){return ''}},document:{querySelector(){return node},querySelectorAll(){return []},body:{style:{}}},
 setTimeout(){return 1},clearTimeout(){},console,window:{},fetch(){throw Error('network forbidden')},FormData:class{}};
vm.createContext(context);vm.runInContext(src,context);
const run=code=>vm.runInContext(code,context);
run(`S.id='p';S.view={project:{stage:'RELATION',scope_confirmed:true},snapshot:{entities:[{id:'a',name:'用户',kind:'class',code:'person'},{id:'b',name:'终端',kind:'class',code:'terminal'}],attributes:[],relations:[{id:'r',source_id:'a',target_id:'b',predicate:'owns_terminal',code:'owns_terminal',name:'拥有终端'}]},active_jobs:[],jobs_latest:{},extraction_gaps:[],pending_totals:{relation:1}};
 S.pendingData.relation={total:1,items:[{id:'q',kind:'relation',stage:'RELATION',source:'rules',payload:{name:'拥有终端',code:'owns_terminal',predicate:'owns_terminal',source_id:'a',target_id:'b'},evidence:[]}]};
 modal=(title,body,buttons)=>{globalThis.lastModal={title,body,buttons};};`);
let html=run(`renderModelTab('relation')`);
assert.ok(html.includes('拥有终端'));
assert.ok(html.includes('英文 CODE：<code>owns_terminal</code>'));
assert.ok(html.includes('全部同意（跨页 1 条）'));
assert.equal(run(`relationDisplay({predicate:'rel_99'})`),'待补充中文名称');
for(const kind of ['entity','attribute','relation']){
 run(`showCandidateForm('${kind}')`);
 const body=run('lastModal.body');
 assert.ok(body.includes('data-key="name"'));
 assert.ok(body.includes('data-key="code"'));
 assert.ok(body.includes('中文名称'));
}
html=run(`renderGraph()`);
assert.ok(html.includes('拥有终端'));
assert.ok(!html.includes('>owns_terminal</text>'));
const escaped=run(`codeLine({code:'<script>alert(1)</script>'})`);
assert.ok(!escaped.includes('<script>'));
console.log('Chinese naming UI, separate editable code, graph label, preserved all-approve, escaped content: PASS');
