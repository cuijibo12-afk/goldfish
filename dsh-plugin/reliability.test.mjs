import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply } from './index.js';
import { Outbox } from './outbox.js';

const human = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] });
const injection = '<goldfish-memory>旧经历的具体细节</goldfish-memory>';
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(config = {}) {
  const hooks = new Map();
  apply({ on: (name, callback) => hooks.set(name, callback), logger: { warn() {} } }, { mode: 'legacy_auto', autoCapture: false, ...config });
  return hooks;
}

test('payload agent is used; decision metadata and original input order survive', async () => {
  const before = globalThis.fetch;
  const message = human('工地');
  const trailing = { ...human('context'), source: { kind: 'plugin:other', form: 'context' } };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, injection }) });
  try {
    const hook = setup().get('agent/pre-step');
    const decision = await hook({ agent: { session: { deriveMessages: () => [] } }, messages: [message] },
      async () => ({ kind: 'enter', startsRequestSeries: true, messages: [message, trailing] }));
    assert.equal(decision.startsRequestSeries, true);
    assert.equal(decision.messages[0], message);
    assert.equal(decision.messages[1].source.kind, 'plugin:goldfish-memory');
    assert.equal(decision.messages[2], trailing);
  } finally { globalThis.fetch = before; }
});

test('restored tool continuation does not recall from old human history', async () => {
  const before = globalThis.fetch;
  let payload;
  globalThis.fetch = async (_, options) => {
    payload = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true, injection }) };
  };
  try {
    const decision = await setup().get('agent/pre-step')({
      agent: { session: { deriveMessages: () => [human('回想工地的事'), { role: 'user', source: { kind: 'compact' }, content: '压缩保留事实' }] } },
      messages: [],
    }, async () => ({ kind: 'enter', messages: [] }));
    assert.equal(payload, undefined);
    assert.equal(decision.messages.length, 0);
  } finally { globalThis.fetch = before; }
});

test('cancellation while recall is pending never admits returned memory', async () => {
  const before = globalThis.fetch;
  const cancel = new AbortController();
  const message = human('工地');
  let observed;
  globalThis.fetch = async (_, options) => {
    observed = options.signal;
    cancel.abort();
    return { ok: true, json: async () => ({ ok: true, injection }) };
  };
  try {
    const original = { kind: 'enter', messages: [message] };
    const result = await setup().get('agent/pre-step')({ agent: { session: { deriveMessages: () => [] } }, messages: [message], signal: cancel.signal }, async () => original);
    assert.equal(result, original);
    assert.equal(observed.aborted, true);
  } finally { globalThis.fetch = before; }
});

test('durable outbox survives HTTP failure and process recreation in event order', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-outbox-'));
  try {
    const first = new Outbox(directory, async () => ({ ok: false }));
    first.enqueue('/schedule', { session_id: 's' });
    first.enqueue('/ingest', { session_id: 's', message_id: 's:10', event_seq: 10 });
    first.enqueue('/ingest', { session_id: 's', message_id: 's:2', event_seq: 2 });
    await first.drain();
    await first.close();
    assert.equal(readdirSync(directory).length, 3);
    if (process.platform !== 'win32') assert.equal(statSync(join(directory, readdirSync(directory)[0])).mode & 0o777, 0o600);
    const delivered = [];
    const second = new Outbox(directory, async (path, payload) => {
      delivered.push(payload.message_id || path);
      return { ok: true };
    });
    await second.drain();
    assert.deepEqual(delivered, ['s:2', 's:10', '/schedule']);
    assert.equal(readdirSync(directory).length, 0);
    await second.close();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('an acknowledgement does not erase a newer queued schedule', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-outbox-'));
  try {
    let once = true;
    const queue = new Outbox(directory, async () => {
      if (once) { once = false; queue.enqueue('/schedule', { session_id: 's' }); }
      return { ok: true };
    });
    queue.enqueue('/schedule', { session_id: 's' });
    await queue.drain();
    assert.equal(readdirSync(directory).length, 1);
    await queue.drain();
    assert.equal(readdirSync(directory).length, 0);
    await queue.close();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('unreadable queue record is retained while valid records still deliver', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-outbox-'));
  try {
    const queue = new Outbox(directory, async () => ({ ok: true }));
    writeFileSync(join(directory, 'damaged.json'), '{');
    queue.enqueue('/ingest', { session_id: 's', message_id: 's:1' });
    await queue.drain();
    assert.deepEqual(readdirSync(directory), ['damaged.json']);
    await queue.close();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('only genuine committed dialogue enters outbox; plugin/tool/attempts do not', async () => {
  const before = globalThis.fetch;
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-capture-'));
  globalThis.fetch = async () => { throw Error('offline'); };
  const hooks = setup({ autoCapture: true, outboxDir: directory });
  try {
    await tick();
    const event = hooks.get('session/event');
    const session = { id: 's', inheritedEventCount: 1 };
    const emit = (seq, type, data) => event(session, { seq, time: 1730000000000, type, data });
    emit(0, 'user/message', human('fork inherited'));
    emit(1, 'user/message', { ...human('memory'), source: { kind: 'plugin:goldfish-memory', form: 'recall' } });
    emit(2, 'user/message', { ...human('tool'), source: { kind: 'tool' } });
    emit(3, 'assistant/attempt', { message: { content: 'failed speculative text' } });
    emit(4, 'user/message', human('我在解释 <goldfish-memory> 标签的含义'));
    emit(5, 'assistant/message', { message: { role: 'assistant', content: [{ type: 'text', text: '实际已交付的文本' }] }, interrupted: true });
    await hooks.get('dispose')();
    assert.equal(readdirSync(directory).filter(name => name.endsWith('.json')).length, 2);
  } finally {
    globalThis.fetch = before;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('restored history uses paged persistence, skips inherited events and contains close errors', async () => {
  const before = globalThis.fetch;
  const directory = mkdtempSync(join(tmpdir(), 'goldfish-backfill-'));
  const hooks = new Map();
  const reads = [];
  const events = Array.from({ length: 260 }, (_, seq) => ({ seq, time: 1730000000000,
    type: 'user/message', data: human('恢复证据' + seq) }));
  globalThis.fetch = async () => { throw Error('offline'); };
  apply({ on: (name, callback) => hooks.set(name, callback), logger: { warn() {} },
    sessionPersistence: { open: async (id, access) => {
      assert.equal(id, 'restored');
      assert.equal(access, 'read');
      return { inheritedEventCount: 2,
        read: async (offset, count) => { reads.push([offset, count]); return { events: events.slice(offset, offset + count) }; },
        close: async () => { throw Error('storage close failure'); },
      };
    } },
  }, { mode: 'legacy_auto', outboxDir: directory });
  try {
    await hooks.get('agent/created')({ agent: { session: { id: 'restored', inheritedEventCount: 2, firstLiveSeq: 260 } } });
    await hooks.get('dispose')();
    assert.deepEqual(reads, [[2, 256], [258, 2]]);
    assert.equal(readdirSync(directory).filter(name => name.endsWith('.json')).length, 258);
  } finally {
    globalThis.fetch = before;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('compaction during the same turn restores cached memory without another model call', async () => {
  const before = globalThis.fetch;
  const payloads = [];
  globalThis.fetch = async (_, options) => {
    payloads.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ ok: true, injection }) };
  };
  try {
    const hook = setup().get('agent/pre-step');
    const message = human('旧码头桂花拿铁');
    let history = [];
    const agent = { session: { deriveMessages: () => history } };
    await hook({ agent, messages: [message] }, async () => ({ kind: 'enter', messages: [message] }));
    history = [{ role: 'user', source: { kind: 'compact' }, content: '往事摘要，具体细节已丢失' }];
    const result = await hook({ agent, messages: [] }, async () => ({ kind: 'enter', messages: [] }));
    assert.equal(payloads.length, 1);
    assert.equal(result.messages.length, 1);
    assert.equal(result.messages[0].content[0].text, injection);
  } finally { globalThis.fetch = before; }
});

test('coverage includes the whole effective history beyond the old 70k suffix', async () => {
  const before = globalThis.fetch;
  let payload;
  globalThis.fetch = async (_, options) => {
    payload = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true, injection: '' }) };
  };
  try {
    const message = human('继续说码头');
    const history = [{ role: 'user', source: { kind: 'plugin:goldfish-memory' },
      content: [{ type: 'text', text: 'old fact near beginning' }] },
      { role: 'assistant', content: [{ type: 'text', text: 'x'.repeat(80000) }] }];
    await setup().get('agent/pre-step')({ agent: { session: { deriveMessages: () => history } }, messages: [message] },
      async () => ({ kind: 'enter', messages: [message] }));
    assert(payload.context.startsWith('old fact near beginning'));
    assert(payload.context.length > 80000);
  } finally { globalThis.fetch = before; }
});
