'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const ctx={console,sessionStorage:{getItem(){return ''}},document:{querySelector(){}},clearTimeout(){},FormData:class{append(key,val){this.file=val}}};
vm.createContext(ctx);
vm.runInContext(source.slice(0,source.indexOf('function progressTitle(phase)')),ctx);
vm.runInContext(source.slice(source.indexOf('async function showSupportedFormats(){'),source.indexOf('async function showReadingBoundaries(){')),ctx);
vm.runInContext(source.slice(source.indexOf('async function uploadFiles(files){'),source.indexOf('async function browseBaseline(kind){')),ctx);
ctx.calls=[];ctx.modals=[];ctx.reloads=[];
vm.runInContext(`S.id='p';toast=function(){};closeModal=function(){};renderContent=function(){};
loadProject=async function(id){reloads.push(id)};
modal=function(title,body){modals.push({title,body})};
api=async function(url,options){
 if(url==='/formats')return {notice:'格式不是含义',formats:[{extension:'.yaml',label:'YAML',available:true,note:'安全读取'},
  {extension:'.doc',label:'DOC',available:false,adapter:'optional_converter',requires:'docling',note:'<script>bad</script>'}]};
 calls.push(options.body.file.name);
 if(options.body.file.name==='坏<文件>.txt')throw Error('无效格式');
 return {queued:options.body.file.name==='pending.docx'};
}`,ctx);
(async()=>{
 await vm.runInContext('showSupportedFormats()',ctx);
 assert.ok(ctx.modals[0].body.includes('可读取'));
 assert.ok(ctx.modals[0].body.includes('需转换/另行验证'));
 assert.ok(!ctx.modals[0].body.includes('<script>bad</script>'));
 await vm.runInContext(`uploadFiles([{name:'good.yaml'},{name:'坏<文件>.txt'},{name:'pending.docx'}])`,ctx);
 assert.equal(ctx.calls.length,3);
 assert.equal(ctx.reloads.length,1);
 assert.ok(ctx.modals[1].body.includes('成功接收 2 个'));
 assert.ok(ctx.modals[1].body.includes('失败 1 个'));
 assert.ok(ctx.modals[1].body.includes('待后台解析'));
 assert.ok(ctx.modals[1].body.includes('坏&lt;文件&gt;.txt'));
 console.log('Formats UI + mixed upload partial success: PASS');
})().catch(err=>{console.error(err);process.exitCode=1});
