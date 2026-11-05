import test from 'node:test';
import assert from 'node:assert/strict';
import { discover, nativeOrganize, replaceWorker } from './compaction.js';
const header = { id: 's' };
test('host reload joins previous organizer before a replacement consumes its queue',async()=>{
  const trace=[];
  const releaseA=await replaceWorker('test-reload',async()=>{trace.push('cancel-old');await new Promise(r=>setTimeout(r,10));trace.push('old-joined');});
  const releaseB=await replaceWorker('test-reload',async()=>{trace.push('cancel-new');});
  trace.push('new-start');releaseA();
  const releaseC=await replaceWorker('test-reload',async()=>{});
  assert.deepEqual(trace,['cancel-old','old-joined','new-start','cancel-new']);releaseB();releaseC();
});
function history() {
  const events = [];
  const add = (type, data) => { const e = { type, data, seq: events.length, time: 1000+events.length }; events.push(e); return e.seq; };
  add('turn/start', { turn: 1 });
  add('request/header', { header: { config: { provider: 'current', model: 'main' } } });
  add('user/message', { id: 'human', source: { kind: 'user' }, content: [{ type: 'text', text: '我的一次出行经历😀。' }] });
  add('user/message', { id: 'guide', source: { kind: 'runtime-context' }, content: [{ type: 'text', text: '模型预测污染' }] });
  add('assistant/message', { message: { content: [{ type: 'text', text: '助手推测污染' }] } });
  add('turn/end', { reason: { kind: 'completed' } });
  const compact = (id, seqs) => {
    add('compaction/start', { compactionId: id });
    add('compaction/summary', { compactionId: id, shadowedSeqs: seqs });
    const checkpoint = add('user/message', { id: 'checkpoint-'+id, source: { kind: 'compact-checkpoint', compactionId: id }, content: [{ type: 'text', text: '被改写的摘要' }] });
    add('compaction/end', { compactionId: id }); return checkpoint;
  };
  return { events, add, compact };
}
test('exact lineage recursively expands compacted checkpoints, excludes non-human surfaces', () => {
  const h = history(); const cp = h.compact('c1', [2,3,4]); h.compact('c2', [cp]);
  const jobs = discover(h.events, header, 0, 'n', 0);
  assert.equal(jobs.length, 2);
  assert.deepEqual(jobs.map(j => j.sources.map(p => p.source.event_seq)), [[2],[2]]);
  assert.equal(jobs[0].sources[0].end, Array.from('我的一次出行经历😀。').length);
  assert.deepEqual(jobs[0].target, { provider: 'current', model: 'main' });
});
test('failed, cancelled, pending, inherited and subagent histories cannot be ingested', () => {
  const h = history(); h.compact('c1', [2,3,4]);
  h.events[5].data.reason.kind = 'cancelled';
  assert.equal(discover(h.events, header, 0, 'n', 0)[0].sources.length, 0);
  h.events[5].type = 'technical';
  assert.equal(discover(h.events, header, 0, 'n', 0).length, 0);
  h.events[5].type = 'turn/end'; h.events[5].data.reason.kind = 'completed';
  assert.equal(discover(h.events, header, 3, 'n', 0)[0].sources.length, 0);
  assert.equal(discover(h.events, { ...header, origin: 'subagent' }, 0, 'n', 0).length, 0);
  h.events.at(-1).data.error = { message: 'failed' };
  assert.equal(discover(h.events, header, 0, 'n', 0).length, 0);
});
test('explicit opt-out vetoes whole human message, activation does not backfill prior compactions', () => {
  const h = history(); h.events[2].data.content[0].text = '我去过松溪站。这件事不要保存为记忆。'; h.compact('c1', [2]);
  assert.equal(discover(h.events, header, 0, 'n', 0)[0].sources.length, 0);
  assert.equal(discover(h.events, header, 0, 'n', 99999).length, 0);
});
test('native call preserves route, has no tools/Agent and ignores hidden reasoning', async () => {
  let seen;
  const llm = { async *stream(options) { seen = options;
    yield { type: 'block-end', block: { type: 'reasoning', text: 'must not retain private chain' } };
    yield { type: 'block-end', block: { type: 'text', text: '{"summaries":[],"profiles":[],"links":[]}' } };
    yield { type: 'usage', usage: { inputTokens: 20, outputTokens: 12 } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  } };
  const result = await nativeOrganize(llm, { target: { provider: 'original', model: 'main' }, context: {}, session_id: 's' });
  assert.equal(seen.provider, 'original'); assert.equal(seen.tools, undefined);
  assert.equal(result.metrics.model_calls, 1); assert(!JSON.stringify(result).includes('private chain'));
});
