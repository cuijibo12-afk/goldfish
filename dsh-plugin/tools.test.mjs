import test from 'node:test';
import assert from 'node:assert/strict';
import { apply } from './index.js';

test('off mode does not register tools, hooks or call a backend', () => {
  apply({ on: () => assert.fail('off mode hook') }, { mode: 'off' });
});

test('native tools mode is optional, source-linked and degrades on service outage',
  { skip: !process.env.DSH_TEST_RUNTIME && 'set DSH_TEST_RUNTIME for the real DSH SDK' }, async () => {
    const registered = [], hooks = new Map(), calls = [];
    const before = globalThis.fetch;
    globalThis.fetch = async (url, options) => {
      calls.push([url, JSON.parse(options.body)]);
      return { ok: true, json: async () => ({ ok: true, records: [{ id: 'memory', evidence_refs: [{ id: 'source' }] }], next_cursor: null }) };
    };
    try {
      await apply({ tools: { register: tool => registered.push(tool) }, on: (event, fn) => hooks.set(event, fn) }, { autoCapture: false });
      assert.deepEqual(registered.map(tool => tool.name), ['goldfish_search', 'goldfish_explore', 'goldfish_evidence']);
      assert(!hooks.has('agent/pre-step'));
      assert.equal(calls.length, 0, 'registering tools does not perform retrieval');
      const exec = { signal: new AbortController().signal };
      const first = JSON.parse(await registered[0].execute({ query: '需要的经历' }, exec));
      await registered[1].execute({ memory_ids: [first.records[0].id] }, exec);
      await registered[2].execute({ memory_id: first.records[0].id }, exec);
      assert.equal(calls.length, 3);
      assert(calls[0][0].endsWith('/search'));
      assert(calls[1][0].endsWith('/explore'));
      assert(calls[2][0].endsWith('/memory_evidence'));
      globalThis.fetch = async () => { throw Error('private data must not appear'); };
      const failed = JSON.parse(await registered[0].execute({ query: '重试' }, exec));
      assert.equal(failed.ok, false);
      assert.equal(failed.degraded, true);
      assert(!JSON.stringify(failed).includes('private data'));
    } finally { globalThis.fetch = before; }
  });
