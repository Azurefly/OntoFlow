/* Run actual settings-render and save handlers, with a tiny DOM/API test double. */
'use strict';
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const config={demo_mode:true,allowed_hosts:[],base_url:'http://host.docker.internal:8080',
 auth_mode:'login',username:'integration',has_password:true,has_access_token:false,
 configured:true,source:'saved',has_admin_token:false};
const nodes=new Map();
let rendered='';let saved=null;
const context={
 S:{id:null,demoMode:false,modelAdminToken:'',openmindAdminToken:''},
 esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 api:async (url,options={})=>{if(options.method==='PUT')saved=JSON.parse(options.body);return config;},
 modal:(title,html)=>{
   rendered=html;nodes.clear();
   for(const match of html.matchAll(/id="([^\"]+)"/g)){
     const id=match[1];
     nodes.set('#'+id,{value:'',checked:false,classList:{toggle(){}},onclick:null});
   }
   nodes.get('#omAuthMode').value='login';
   nodes.get('#omBaseUrl').value=config.base_url;
   nodes.get('#omUsername').value='integration';
 },
 $:selector=>nodes.get(selector)||null,
 document:{querySelectorAll(){return [];}},
 task:async fn=>fn(),omAdminHeaders:()=>({}),closeModal(){},toast(){},console
};
vm.createContext(context);
const from=source.indexOf('function openMindHostSettings(config){');
const until=source.indexOf('function omAdminHeaders()',from);
assert.ok(from>=0&&until>from);
vm.runInContext(source.slice(from,until),context);
(async()=>{
 await vm.runInContext('openOpenMind()',context);
 assert.equal(context.S.demoMode,true); // config corrects initial stale UI mode
 assert.ok(!rendered.includes('id="omAllowedHosts"'));
 assert.ok(!rendered.includes('id="omAdmin"'));
 assert.ok(rendered.includes('demo_unrestricted'));
 assert.ok(rendered.includes('支持 HTTP / HTTPS'));
 await nodes.get('#omSaveConfig').onclick();
 assert.equal(saved.base_url,config.base_url);
 assert.equal(saved.username,'integration');
 assert.equal(saved.auth_mode,'login');
 assert.ok(!Object.hasOwn(saved,'allowed_hosts'));
 config.demo_mode=false;config.allowed_hosts=['strict.example'];
 const html=vm.runInContext('openMindHostSettings({demo_mode:false,allowed_hosts:["strict.example"],http_compat_enabled:false})',context);
 assert.ok(html.includes('id="omAllowedHosts"'));
 assert.ok(html.includes('strict.example'));
 console.log('Demo OpenMind rendering + save without whitelist: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
