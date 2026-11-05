import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { apply } from './index.js';
import { Outbox } from './outbox.js';

const human = (id, text = '同一句请求') => ({ id, role: 'user', source: { kind: 'user' }, content: text });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('creating a fresh nested outbox completes on Windows instead of looping at the path root', () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-new-queue-'));
  try {
    const script = `import { Outbox } from ${JSON.stringify(new URL('./outbox.js', import.meta.url).href)};
      const q = new Outbox(process.argv[1], async () => ({ok:true})); await q.close();`;
    const child = spawnSync(process.execPath, ['--input-type=module', '-e', script, join(directory, 'new', 'queue')],
      { encoding: 'utf8', timeout: 5000 });
    assert.equal(child.error, undefined);
    assert.equal(child.status, 0, child.stderr);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('twenty tool steps use one recall; identical text with a new message id recalls again', async () => {
  const hooks = new Map();
  const before = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: true, json: async () => ({ ok: true, injection: '已核验的记忆' }) };
  };
  try {
    apply({ on: (event, cb) => hooks.set(event, cb) }, { mode: 'legacy_auto', autoCapture: false });
    const hook = hooks.get('agent/pre-step');
    let history = [];
    const agent = { session: { deriveMessages: () => history } };
    const step = async (messages, turn = 1) => hook({ agent, messages, turn }, async () => ({ kind: 'enter', messages }));
    const first = human('u1');
    const result = await step([first]);
    history = result.messages;
    for (let i = 0; i < 20; i++) assert.equal((await step([])).messages.length, 0);
    await step([{ ...first }]);
    assert.equal(calls, 1);
    history = [{ role: 'user', source: { kind: 'compact-checkpoint' }, content: '压缩后的上下文' }];
    assert.equal((await step([])).messages.at(-1).source.kind, 'plugin:goldfish-memory');
    assert.equal(calls, 1);
    assert.equal((await step([], 2)).messages.length, 0, 'cache never crosses turn boundaries');
    await step([human('u2')], 2);
    assert.equal(calls, 2);
    await step([human('u3', '任务中途补充要求')], 2);
    assert.equal(calls, 3, 'genuine steering input gets its own recall');
  } finally { globalThis.fetch = before; }
});

test('offline recall makes one attempt per human input, not one attempt per tool step', async () => {
  const hooks = new Map();
  const before = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw Error('offline'); };
  try {
    apply({ on: (event, cb) => hooks.set(event, cb) }, { mode: 'legacy_auto', autoCapture: false });
    const message = human('u1');
    const agent = { session: { deriveMessages: () => [message] } };
    const hook = hooks.get('agent/pre-step');
    await hook({ agent, messages: [message] }, async () => ({ kind: 'enter', messages: [message] }));
    for (let i = 0; i < 10; i++) await hook({ agent, messages: [] }, async () => ({ kind: 'enter', messages: [] }));
    assert.equal(calls, 1);
  } finally { globalThis.fetch = before; }
});

test('partial sources survive recreation; a completed boundary releases only that turn', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-turn-boundary-'));
  const delivered = [];
  let queue;
  try {
    const first = new Outbox(directory, async () => { throw Error('must not deliver'); });
    first.enqueue('/ingest', { session_id: 's', message_id: 's:1', event_seq: 1 }, { deferUntilSchedule: true });
    await first.drain();
    await first.close();
    assert.equal(readdirSync(directory).length, 1);
    queue = new Outbox(directory, async (path, payload) => {
      delivered.push([path, payload.event_seq]);
      return { ok: true };
    });
    queue.enqueue('/ingest', { session_id: 's', message_id: 's:5', event_seq: 5 }, { deferUntilSchedule: true });
    queue.enqueue('/ingest', { session_id: 'other', message_id: 'other:1', event_seq: 1 }, { deferUntilSchedule: true });
    queue.enqueue('/schedule', { session_id: 's', event_seq: 3 });
    await queue.drain();
    await queue.drain();
    assert.deepEqual(delivered, [['/ingest', 1], ['/schedule', 3]]);
    assert.equal(readdirSync(directory).length, 2);
    queue.enqueue('/schedule', { session_id: 's', event_seq: 7 });
    await queue.drain();
    assert.deepEqual(delivered.slice(2), [['/ingest', 5], ['/schedule', 7]]);
    assert.equal(readdirSync(directory).length, 1, 'other active session stays deferred');
  } finally { await queue?.close(); rmSync(directory, { recursive: true, force: true }); }
});

test('intermediate assistant text is durable but schedules only on turn end or disposal', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-turn-capture-'));
  const hooks = new Map();
  const before = globalThis.fetch;
  const delivered = [];
  globalThis.fetch = async (url, options) => {
    delivered.push({ path: new URL(url).pathname, ...JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  try {
    apply({ on: (event, cb) => hooks.set(event, cb) }, { mode: 'legacy_auto', outboxDir: directory, retryMs: 100 });
    const session = { id: 's', seq: 0 };
    const emit = (seq, type, data) => {
      session.seq = seq + 1;
      hooks.get('session/event')(session, { seq, time: 1730000000000, type, data });
    };
    emit(0, 'user/message', human('u1'));
    for (let i = 1; i <= 20; i++) emit(i, 'assistant/message', { message: { role: 'assistant', content: '执行进展' } });
    await tick(); await tick();
    assert.equal(delivered.length, 0);
    assert.equal(readdirSync(directory).length, 21, 'raw dialogue is already persisted');
    emit(21, 'turn/end', { turn: 1, reason: { kind: 'completed' } });
    for (let i = 0; i < 100 && !delivered.some(item => item.path === '/schedule'); i++) await tick();
    assert.equal(delivered.filter(item => item.path === '/schedule').length, 1);
    assert.equal(delivered.filter(item => item.path === '/ingest').length, 21);
    assert(delivered.filter(item => item.path === '/ingest').every(item => item.consolidate_after === false));
    emit(22, 'user/message', human('u2'));
    hooks.get('session/disposed')(session);
    for (let i = 0; i < 100 && delivered.filter(item => item.path === '/schedule').length < 2; i++) await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(delivered.filter(item => item.path === '/schedule').length, 2);
  } finally {
    await hooks.get('dispose')();
    globalThis.fetch = before;
    rmSync(directory, { recursive: true, force: true });
  }
});
