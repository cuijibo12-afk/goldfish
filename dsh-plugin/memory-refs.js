/** Compact read references; real IDs stay intact, nothing is written to memory. */
import { createHash } from 'node:crypto';
const READS = new Set(['goldfish_search', 'goldfish_explore', 'goldfish_evidence']);
export const memoryRef = id => 'gf_' + createHash('sha256').update(id).digest('hex').slice(0, 16);
export function observedMemories(messages, guideState) {
  const calls = new Set(), ids = new Set();
  for (const guide of [guideState?.current, guideState?.active?.draft]) {
    for (const item of guide?.content?.progress || []) {
      if (item.kind === 'memory_statement') for (const id of item.source_ids) ids.add(id);
    }
  }
  for (const message of messages) {
    if (message.role === 'assistant') for (const b of message.content || [])
      if (b.type === 'tool-call' && READS.has(b.name)) calls.add(b.id);
    if (message.role !== 'tool' || !calls.has(message.toolCallId)) continue;
    try {
      const result = JSON.parse(message.content.filter(b => b.type === 'text').map(b => b.text).join(''));
      if (!result.ok) continue;
      // Evidence records contain raw source IDs rather than memory IDs.
      if (typeof result.memory_id === 'string') ids.add(result.memory_id);
      else for (const r of result.records || []) if (typeof r.id === 'string') ids.add(r.id);
    } catch { /* A non-Goldfish or malformed result grants no alias. */ }
  }
  return [...ids].slice(-100);
}
export function resolveMemoryRef(value, ids) {
  if (typeof value !== 'string' || !/^gf_[a-f0-9]{16}$/.test(value)) return value;
  const matches = ids.filter(id => memoryRef(id) === value);
  if (matches.length !== 1) throw Error('unknown_or_ambiguous_memory_ref');
  return matches[0];
}
export function addMemoryRefs(result, evidence = false) {
  return { ...result,
    ...(typeof result.memory_id === 'string' ? { memory_ref: memoryRef(result.memory_id) } : {}),
    records: result.records.map(r => evidence || typeof r.id !== 'string' ? r : { ...r, memory_ref: memoryRef(r.id) }) };
}
