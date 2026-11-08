/* Regression for resuming the persisted workflow stage in the actual frontend JS. */
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname,'..','static','app.js'),'utf8');
const marker = 'function progressTitle(phase)';
assert.ok(script.includes(marker));
const projectRows = [
  {id:'entity-project',name:'设备构建',stage:'ENTITY',revision:3,published_version:0},
  {id:'relation-project',name:'关系构建',stage:'RELATION',revision:6,published_version:0},
  {id:'review-project',name:'准备发布',stage:'REVIEW',revision:7,published_version:0},
  {id:'topic-project',name:'概要确认',stage:'TOPIC',revision:1,published_version:0}
];
const nodes = new Map(projectRows.map(p=>[p.id,{dataset:{project:p.id},onclick:null}]));
const list = {innerHTML:'',querySelectorAll(selector){return selector==='[data-project]'?[...nodes.values()]:[];}};
const context = {
  sessionStorage:{getItem(){return '';}},
  clearTimeout(){},
  document:{querySelector(selector){return selector==='#projectList'?list:null;}},
  console
};
vm.createContext(context);
vm.runInContext(script.slice(0,script.indexOf(marker)),context);
// Load the real click-handler implementation without executing browser bootstrapping.
vm.runInContext(script.slice(script.indexOf('function renderProjects(){'),script.indexOf('function confirmDeleteProject(p){')),context);
context.__rows = Object.fromEntries(projectRows.map(p=>[p.id,p]));
context.__calls = [];
context.__list = list;
vm.runInContext(`
 api=async function(url){const id=url.split('/').at(-1);return {project:__rows[id]};};
 fetchPendingPage=async function(kind){__calls.push(kind);};
 renderAll=function(){};
 resumeJobs=function(){};
 task=async function(fn){return fn();};
`,context);
async function check(){
  const run=js=>vm.runInContext(js,context);
  for(const [stage,expected] of Object.entries({UPLOAD:'overview',TOPIC:'overview',ENTITY:'entity',PROPERTY:'attribute',RELATION:'relation',REVIEW:'review',UNKNOWN:'overview'})){
    assert.equal(run(`tabForStage(${JSON.stringify(stage)})`),expected,stage);
  }
  // Initial open activates the persisted ENTITY stage, not the old overview tab.
  await run('loadProject("entity-project")');
  assert.equal(run('S.tab'),'entity');
  assert.equal(context.__calls.at(-1),'entity');
  // A manual click to another tab is retained when polling / refresh reloads the same project.
  run('S.tab="overview"');
  await run('loadProject("entity-project")');
  assert.equal(run('S.tab'),'overview');
  // Explicit clicking the same workspace reactivates its actual stage.
  run('S.projects=Object.values(__rows)');
  run('renderProjects()');
  await nodes.get('entity-project').onclick();
  assert.equal(run('S.tab'),'entity');
  // Switching to a different workspace activates its own persisted workflow stage.
  await nodes.get('relation-project').onclick();
  assert.equal(run('S.tab'),'relation');
  assert.equal(context.__calls.at(-1),'relation');
  await nodes.get('review-project').onclick();
  assert.equal(run('S.tab'),'review');
  assert.equal(context.__calls.at(-1),'review');
  await nodes.get('topic-project').onclick();
  assert.equal(run('S.tab'),'overview');
  console.log('Stage restore and per-workspace re-entry: PASS');
}
check().catch(err=>{console.error(err);process.exitCode=1;});
