import test from 'node:test';
import assert from 'node:assert/strict';
import {apply} from './index.js';

test('DSH pre-step injects memory into main model input; never needs a tool',async()=>{
  const hooks=new Map();
  const before=globalThis.fetch;
  globalThis.fetch=async (url,opts)=>({ok:true,json:async()=>({ok:true,injection:'<goldfish-memory>一段有依据的记忆</goldfish-memory>'})});
  try{
    const ctx={on:(name,fn)=>hooks.set(name,fn),agents:{requireInitiator:()=>({session:{deriveMessages:()=>[]}})},logger:{warn:()=>{}}};
    apply(ctx,{autoCapture:false});
    assert(hooks.has('agent/pre-step'));
    assert(!hooks.has('session/event'));
    const message={role:'user',source:{kind:'user'},content:[{type:'text',text:'你还记得那段经历吗'}]};
    const decision=await hooks.get('agent/pre-step')({agent:{session:{deriveMessages:()=>[]}},messages:[message],signal:new AbortController().signal},
      async()=>({kind:'enter',messages:[message]}));
    assert.equal(decision.messages.length,2);
    assert.equal(decision.messages[1].source.kind,'plugin:goldfish-memory');
    assert.match(decision.messages[1].content[0].text, /一段有依据的记忆/);
  }finally{globalThis.fetch=before;}
});

test('Plugin skips injection if local recall is unavailable',async()=>{
  const hooks=new Map();const before=globalThis.fetch;
  globalThis.fetch=async()=>{throw Error('offline')};
  try{
    apply({on:(name,fn)=>hooks.set(name,fn),agents:{requireInitiator:()=>({session:{deriveMessages:()=>[]}})},logger:{warn:()=>{}}},
      {autoCapture:false,timeoutMs:50});
    const msg={role:'user',source:{kind:'user'},content:[{type:'text',text:'测试'}]};
    const decision=await hooks.get('agent/pre-step')({agent:{session:{deriveMessages:()=>[]}},messages:[msg],signal:new AbortController().signal},
      async()=>({kind:'enter',messages:[msg]}));
    assert.equal(decision.messages.length,1);
  }finally{globalThis.fetch=before;}
});
