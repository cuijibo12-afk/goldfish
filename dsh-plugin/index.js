/**
 * Goldfish 0.1 personal DSH context bridge.
 * No agent tools; memory is added to main model-visible input BEFORE inference.
 * Mirrors DSH's public agent/pre-step source-context pattern.
 */
import { randomUUID } from 'node:crypto';
export const name = 'goldfish-memory';
export const inject = ['agents'];

const KIND='plugin:goldfish-memory';
const flatten=(v)=> typeof v==='string'?v:Array.isArray(v)?v.map(b=>
  typeof b==='string'?b:(b && typeof b.text==='string'?b.text:'')).join(''):'';

function lastHumanMessage(messages){
  for(let i=messages.length-1;i>=0;i--){
    const m=messages[i];
    if(m?.role!=='user') continue;
    if(m?.source?.kind && m.source.kind!=='user') continue;
    const text=flatten(m.content).trim();
    if(text && !text.includes('<goldfish-memory>')) return text;
  }
  return '';
}

async function post(url,path,payload,timeoutMs,token){
  const controller = new AbortController();
  const timer = setTimeout(()=>controller.abort(),timeoutMs);
  try {
    const resp = await fetch(url+path, {
      method:'POST',
      headers:{'Content-Type':'application/json',...(token?{'X-Goldfish-Token':token}:{})},
      body:JSON.stringify(payload),signal:controller.signal,
    });
    if(!resp.ok) return null;
    return await resp.json();
  } finally { clearTimeout(timer); }
}

export function apply(ctx,config={}) {
  const url=String(config.serverUrl??'http://127.0.0.1:18320').replace(/\/$/,'');
  const token=config.token??process.env.GOLDFISH_LOCAL_TOKEN??'';
  const timeout=Number(config.timeoutMs??2500);
  const injectedMax=Number(config.maxChars??1800);
  const capture=config.autoCapture!==false;
  if(capture){
    let captureChain=Promise.resolve();
    // Persist only real user turns and actual assistant outputs.
    // This is a local event listener, no model-facing memory tool.
    ctx.on('session/event',(session,event)=>{
      if(!['user/message','assistant/message'].includes(event.type)) return;
      const msg=event.type==='user/message'?event.data:event.data?.message;
      if(!msg) return;
      if(event.type==='user/message' && msg.source?.kind!=='user') return;
      if(event.type==='user/message' && event.data?.source?.kind && event.data.source.kind!=='user') return;
      const text=flatten(msg.content).trim();
      if(!text || text.includes('<goldfish-memory>')) return;
      const role=event.type==='user/message'?'user':'assistant';
      captureChain=captureChain.catch(()=>{}).then(()=>post(url,'/ingest',{
        message_id:String(session.id)+':'+String(event.seq),
        session_id:String(session.id),role,text,
        source:role==='user'?'human':'assistant_observed',
        event_time:Number.isFinite(event.time)?new Date(event.time).toISOString():new Date().toISOString(),
        consolidate_after:role==='assistant',
      },timeout,token)).catch(e=>ctx.logger?.warn('[goldfish] ingest skipped: '+e.message));
    });
    // Session disposal is not a data deletion; flush remaining raw events.
    ctx.on('session/disposed',(session)=>{
      captureChain=captureChain.catch(()=>{}).then(()=>post(url,'/schedule',{session_id:String(session.id)},timeout,token))
        .catch(e=>ctx.logger?.warn('[goldfish] schedule skipped: '+e.message));
    });
  }
  ctx.on('agent/pre-step',async ({messages,signal},next)=>{
    const decision=await next();
    if(decision.kind!=='enter'||signal?.aborted||!Array.isArray(decision.messages))return decision;
    const query=lastHumanMessage(decision.messages);
    if(!query)return decision;
    try{
      const agent=ctx.agents?.requireInitiator?.();
      // Derived surface reflects DSH compaction; do not assume an injected
      // memory from a previous turn is still present after compaction.
      const history=agent?.session?.deriveMessages?.()??[];
      const activeContext=[...history,...decision.messages].map(m=>flatten(m.content))
        .filter(Boolean).join('\n').slice(-50000);
      const data=await post(url,'/recall',{
        query,context:activeContext,max_chars:injectedMax,
        limit:config.limit??5,depth:config.depth??2,
      },timeout,token);
      if(!data?.ok||!data.injection)return decision;
      // DSH v4 requires an owner kind, not the generic 'plugin' kind.
      const injected={role:'user',id:randomUUID(),content:[{type:'text',text:data.injection}],
        source:{kind:KIND,form:'recall'}};
      const anchor=decision.messages.findLastIndex(m=>messages.includes(m));
      const nextMessages=[...decision.messages];
      nextMessages.splice(anchor+1,0,injected);
      return {kind:'enter',messages:nextMessages};
    }catch(e){
      ctx.logger?.warn('[goldfish] recall skipped: '+e.message);
      return decision; // Failed memory search must never break chat.
    }
  });
}
