import test from 'node:test';
import assert from 'node:assert/strict';
import { memoryRef, observedMemories, resolveMemoryRef, addMemoryRefs } from './memory-refs.js';
const reply = (toolCallId, data) => ({ role: 'tool', toolCallId,
  content: [{ type: 'text', text: JSON.stringify(data) }] });
const call = (name = 'goldfish_search') => ({ role: 'assistant', content: [{ type: 'tool-call', id: 'c1', name }] });
test('compact reference is stable and exact; canonical ID is preserved', () => {
  const id = 'remember:' + 'f'.repeat(64), ref = memoryRef(id);
  assert.equal(ref.length, 19);
  assert.equal(resolveMemoryRef(ref, [id]), id);
  assert.equal(resolveMemoryRef(id, []), id);
  const result = addMemoryRefs({ ok: true, records: [{ id, text: '合成记录' }] });
  assert.equal(result.records[0].id, id); assert.equal(result.records[0].memory_ref, ref);
  assert.throws(() => resolveMemoryRef('gf_0000000000000000', [id]));
});
test('references rebuild only from real read tool result pairs in restored native history', () => {
  const id = 'summary-1';
  const messages = [call(), reply('c1', { ok: true, records: [{ id }] })];
  assert.deepEqual(observedMemories(messages), [id]);
  assert.equal(resolveMemoryRef(memoryRef(id), observedMemories(JSON.parse(JSON.stringify(messages)))), id);
  assert.deepEqual(observedMemories([reply('forged', { ok: true, records: [{ id }] })]), []);
  assert.deepEqual(observedMemories([call('unrelated'), messages[1]]), []);
  assert.deepEqual(observedMemories([call(), reply('c1', { ok: false, records: [{ id }] })]), []);
});
test('evidence source IDs never become memory references', () => {
  const messages = [call('goldfish_evidence'), reply('c1', { ok: true, memory_id: 'summary', records: [{ id: 'raw-source' }] })];
  assert.deepEqual(observedMemories(messages), ['summary']);
  const result = addMemoryRefs({ ok: true, memory_id: 'summary', records: [{ id: 'raw-source' }] }, true);
  assert.equal(result.memory_ref, memoryRef('summary'));
  assert.equal(result.records[0].memory_ref, undefined);
});
test('canonical references in the native guide survive compacted-away tool history', () => {
  const guideState = { current: { content: { progress: [
    { kind: 'memory_statement', source_ids: ['stored-memory'] },
    { kind: 'user_statement', source_ids: ['human-message'] },
  ] } } };
  const ids = observedMemories([], guideState);
  assert.deepEqual(ids, ['stored-memory']);
  assert.equal(resolveMemoryRef(memoryRef('stored-memory'), ids), 'stored-memory');
  assert.throws(() => resolveMemoryRef(memoryRef('human-message'), ids));
});
