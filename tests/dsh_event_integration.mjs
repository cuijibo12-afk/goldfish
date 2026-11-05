/** Installed DSH native compaction + real Python/HTTP/schema6; synthetic model only. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createServer} from 'node:http';
import {spawn,execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {hostModule as load} from '../dsh-plugin/host-sdk.js';
import * as Goldfish from '../dsh-plugin/index.js';
import {discover} from '../dsh-plugin/compaction.js';
const root=resolve(fileURLToPath(new URL('..',import.meta.url)));
const [cordis,llm,sessions,projections,prompt,tools,agents,loop,jsonl,meter,compact]=await Promise.all(
 ['cordis','dsh-llm','dsh-session','dsh-session-projection','dsh-system-prompt','dsh-tools','dsh-agent','dsh-agent-loop',
  'dsh-session-persistence-jsonl','dsh-token-meter','dsh-compaction-basic'].map(n=>load('@deepseek-ai/'+n)));
const text=m=>(m.content||[]).filter(b=>b.type==='text').map(b=>b.text).join('');

test('D/E source archive: native compaction organizes one event across batches with own fact evidence', {timeout:45000},async t=>{
 const temp=await mkdtemp(join(tmpdir(),'goldfish-release-events-'));
 const reserve=createServer();reserve.listen(0,'127.0.0.1');await once(reserve,'listening');
 const port=reserve.address().port;await new Promise(r=>reserve.close(r));
 const url=`http://127.0.0.1:${port}`,database=join(temp,'memory.sqlite3');
 const env={...process.env,PYTHONPATH:root,PYTHONUTF8:'1'};
 for(const k of Object.keys(env))if(k.startsWith('GOLDFISH_'))delete env[k];
 const backend=spawn(process.env.GOLDFISH_TEST_PYTHON||'python',['-m','goldfish.server','--port',String(port),
   '--db',database,'--read-only','--allow-remember','--allow-compaction'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 let ctx,diagnostic='',organizerCalls=0;const calls=[],originalFetch=globalThis.fetch;
 globalThis.fetch=async(address,options)=>{const result=await originalFetch(address,options);
  if(String(address).endsWith('/compaction'))calls.push({action:JSON.parse(options.body).action,status:result.status});
  return result;};
 backend.stdout.on('data',b=>diagnostic+=b);backend.stderr.on('data',b=>diagnostic+=b);
 const post=async(path,body)=>{const r=await fetch(url+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(r.ok);return r.json();};
 try{
  for(let i=0;;i++){try{if((await fetch(url+'/health')).ok)break;}catch{}
   if(i>100||backend.exitCode!==null)throw Error('isolated backend startup failed: '+diagnostic);
   await new Promise(r=>setTimeout(r,25));}
  const health=await(await fetch(url+'/health')).json();assert.equal(health.schema,6);assert(health.event_memory);
  const providers=await(await fetch(url+'/providers')).json();assert(!providers.chat.configured&&!providers.embedding.configured);
  ctx=new cordis.Context();for(const p of [llm.default,sessions.default,projections.default,prompt.default,tools.default,agents.default])await ctx.plugin(p);
  await ctx.plugin(jsonl,{root:join(temp,'sessions'),compression:'none'});await ctx.plugin(loop.default,{agents:[]});await ctx.plugin(meter.default);
  class Adapter extends llm.LlmAdapter{
   async resolveModel(provider,model){return {provider,id:model,name:model,context:{contextWindow:100000}};}
   async *stream(options){
    let output='收到';
    if(options.purpose==='goldfish-compaction'){
     organizerCalls++;assert.equal(options.provider,'synthetic');assert.equal(options.model,'fixture');assert.equal(options.tools,undefined);
     const input=JSON.parse(text(options.messages.at(-1)));
     if(input.proposed){const p=input.proposed;output=JSON.stringify({
      events:p.events.map(e=>({ref:e.ref,verdict:'supported',reason:'合成验收：具体事件身份一致'})),
      facts:p.events.flatMap(e=>e.facts.map(f=>({ref:f.ref,verdict:'supported',reason:'合成验收：本批真人原话',
       user_expression:true,recommended_operation:f.operation,target_fact:null,adds_information:true}))),relations:[],revoke:[]});
     }else{
      const fresh=input.messages.filter(m=>m.text.includes('发布测试-A71'));
      const old=input.existing.find(m=>m.event_key==='发布测试-A71');
      output=JSON.stringify({events:fresh.length?[{ref:'new_event',existing:old?.id||null,event_key:'发布测试-A71',
       title:'发布验收虚构：发布测试-A71',theme:'出行准备',anchors:[{source:fresh[0].id,quote:fresh[0].text.trim()}],
       facts:fresh.map((m,i)=>({ref:'new_fact'+i,text:m.text.trim(),aspect:m.text.includes('回程')?'outcome':'context',time:'2022',
        operation:old?'supplement':'new',target_fact:null,evidence:[{source:m.id,quote:m.text.trim()}]}))}]:[],relations:[],revoke:[]});
     }
    }
    yield {type:'block-start',index:0,blockType:'text'};
    yield {type:'block-end',index:0,block:{type:'text',text:output}};
    yield {type:'usage',usage:{inputTokens:10,outputTokens:10,totalTokens:20}};
    yield {type:'finish',reason:{kind:'stop'}};
   }
  }
  ctx.llm.registerAdapter(['synthetic'],new Adapter());
  await ctx.plugin(Goldfish,{serverUrl:url,enableGuide:true,enableRemember:true,enableCompaction:true,autoCapture:false,
   rememberKeyFile:join(temp,'remember-write.key'),compactionKeyFile:join(temp,'compaction-write.key'),
   compactionStateDir:join(temp,'state'),compactionRetryMs:1000});
  // Cordis plugin() returns a Fiber, not completion of asynchronous apply.
  // Wait for registration before creating the synthetic ultra-fast session.
  for(let i=0;!ctx.tools.get('goldfish_organization_status');i++){
   if(i>100)throw Error('compaction tool registration did not become ready');
   await new Promise(r=>setTimeout(r,20));
  }
  const agent=await ctx.agentLoop.create(sessions.SessionId('release-event-test'),{provider:'synthetic',model:'fixture'});
  const send=async(value,kind='user')=>{agent.followup(llm.createUserMessage({source:{kind},content:[{type:'text',text:value}]}));await agent.whenIdle();};
  class SmallSummary extends compact.BasicCompactionEngine{
   async summarize(){return {summary:[{type:'text',text:'合成验收摘要，具体细节省略。'}],provider:'synthetic',model:'fixture'};}
  }
  const engine=new SmallSummary(ctx,{auto:false});
  async function compressAndWait(expected){
   await send('技术填充，不是经历。'.repeat(150),'plugin:release-padding');
   assert(await engine.compactNow(agent,new AbortController().signal),'native compact must succeed');
   for(let i=0;i<100;i++){
    const r=await post('/search',{query:'发布测试-A71',limit:2});
    if(r.records[0]?.fact_count===expected)return r.records[0];
    await new Promise(r=>setTimeout(r,100));
   }
   const state=execFileSync(process.env.GOLDFISH_TEST_PYTHON||'python',['-c',
    'import sqlite3,sys,json; d=sqlite3.connect(sys.argv[1]); print(json.dumps(d.execute("SELECT state,attempts,last_error FROM compact_jobs").fetchall()))',database],{encoding:'utf8'});
   await ctx.sessionPersistence.flush();const handle=await ctx.sessionPersistence.open(agent.session.id,'read');
   let discovery;try{const page=await handle.read();discovery=discover(page.events,handle.header,0,'release-test',0).map(j=>({sources:j.sources.length,target:j.target}));}finally{await handle.close();}
   const statusTool=ctx.tools.get('goldfish_organization_status');
   const status=statusTool?await statusTool.execute({},{agent,signal:new AbortController().signal}):'tool_not_registered';
   throw Error('native compaction organization did not finish; model_calls='+organizerCalls+'; jobs='+state+'; discovered='+JSON.stringify(discovery)+'; calls='+JSON.stringify(calls)+'; status='+status);
  }
  await send('发布验收虚构：2022年发布测试-A71出发前发现防水袋破损。');
  const first=await compressAndWait(1);
  await send('发布验收虚构：补充发布测试-A71，回程后把地图放在蓝柜第四格。');
  const second=await compressAndWait(2);assert.equal(first.id,second.id);
  const evidence=await post('/memory_evidence',{memory_id:second.id});
  assert.equal(new Set(evidence.records.map(r=>r.fact_id)).size,2);
  assert.equal(new Set(evidence.records.map(r=>r.id)).size,2);
  assert(evidence.records.some(r=>r.text.includes('蓝柜第四格')));
  assert(evidence.records.every(r=>!r.text.includes('技术填充')));
  assert.equal(organizerCalls,4);
  t.diagnostic('Real installed DSH native compaction + HTTP/schema6 passed; scripted proposal/review only, no credentials or real model inference.');
 }finally{
  await ctx?.fiber.dispose();globalThis.fetch=originalFetch;const ended=once(backend,'exit');backend.kill();await ended;
  await rm(temp,{recursive:true,force:true});
 }
});
