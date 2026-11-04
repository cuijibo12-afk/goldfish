/** Goldfish: automatic recall through DSH's public pre-step lifecycle. */
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { Outbox } from './outbox.js';

export const name = 'goldfish-memory';
export const inject = ['agents'];
const KIND = 'plugin:goldfish-memory';
// Cordis service property getters require declared inject dependencies. Optional
// facilities must use its public optional service lookup, not optional chaining.
const optionalService = (ctx, name) => typeof ctx.get === 'function' ? ctx.get(name) : ctx[name];
const flatten = value => typeof value === 'string' ? value : Array.isArray(value)
  ? value.filter(block => block?.type === 'text').map(block => block.text ?? '').join('') : '';

function lastHumanMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (message?.role !== 'user' || message.source?.kind !== 'user') continue;
    const text = flatten(message.content).trim();
    if (text) return text;
  }
  return '';
}

async function post(url, path, payload, timeoutMs, token, signal) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url + path, {
      method: 'POST', redirect: 'error',
      headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Goldfish-Token': token } : {}) },
      body: JSON.stringify(payload),
      signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
    });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return await response.json();
  } finally { clearTimeout(timer); }
}

export function apply(ctx, config = {}) {
  const url = String(config.serverUrl ?? 'http://127.0.0.1:18320').replace(/\/$/, '');
  const endpoint = new URL(url);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password
      || endpoint.search || endpoint.hash) throw new Error('Invalid Goldfish server URL');
  if (endpoint.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)) {
    throw new Error('Remote Goldfish servers require HTTPS');
  }
  const token = config.token ?? process.env.GOLDFISH_LOCAL_TOKEN ?? '';
  const timeout = Math.max(50, Math.min(Number(config.timeoutMs) || 2500, 30000));
  const maxChars = Math.max(250, Math.min(Number(config.maxChars) || 1800, 8000));
  const warn = message => ctx.logger?.warn('[goldfish] ' + message);
  // Compaction inside a tool chain can remove its triggering human message.
  // This holds only a retrieval cue, never an injection acknowledgement or fact.
  const lastQueries = new WeakMap();

  if (config.autoCapture !== false) {
    try {
      const home = optionalService(ctx, 'pluginPackages')?.resolution?.profileDir
        || process.env.DSH_HOME || join(homedir(), '.dsh');
      const namespace = createHash('sha256').update(url).digest('hex').slice(0, 16);
      const directory = config.outboxDir ?? join(home, 'goldfish-memory', 'outbox', namespace);
      const outbox = new Outbox(directory, (path, payload) => post(url, path, payload, timeout, token), warn);
      let closed = false;
      const backfills = new Set();
      const capture = (session, event) => {
        // A fork's inherited prefix belongs to its original session.
        if (event.seq < (session.inheritedEventCount ?? 0)) return;
        if (!['user/message', 'assistant/message'].includes(event.type)) return;
        const message = event.type === 'user/message' ? event.data : event.data?.message;
        if (!message || (event.type === 'user/message' && message.source?.kind !== 'user')) return;
        const text = flatten(message.content);
        if (!text.trim()) return;
        const role = event.type === 'user/message' ? 'user' : 'assistant';
        outbox.enqueue('/ingest', {
          message_id: String(session.id) + ':' + String(event.seq), session_id: String(session.id),
          role, text, source: role === 'user' ? 'human' : 'assistant_observed',
          event_time: new Date(event.time).toISOString(), event_seq: event.seq,
          consolidate_after: role === 'assistant',
        });
      };
      const safelyCapture = (session, event) => {
        try { capture(session, event); }
        catch { warn('local event write failed; recover from DSH persistence on next resume'); }
      };
      ctx.on('session/event', (session, event) => {
        safelyCapture(session, event);
        void outbox.drain();
      });
      // Restored events are absent from session/event. Read the official paged
      // persistence API rather than deprecated synchronous log snapshots.
      ctx.on('agent/created', async ({ agent }) => {
        const persistence = optionalService(ctx, 'sessionPersistence');
        if (!persistence) {
          warn('DSH persistence unavailable; historical backfill cannot run');
          return;
        }
        const task = (async () => {
          let handle;
          try {
            handle = await persistence.open(agent.session.id, 'read');
            let offset = handle.inheritedEventCount ?? 0;
            const end = agent.session.firstLiveSeq ?? agent.session.seq;
            while (!closed && offset < end) {
              const { events } = await handle.read(offset, Math.min(256, end - offset));
              if (!events.length) break;
              for (const event of events) {
                safelyCapture(agent.session, event);
                if (event.type === 'user/message' && event.data?.source?.kind === 'user') {
                  const query = flatten(event.data.content).trim();
                  if (query) lastQueries.set(agent, query);
                }
              }
              offset += events.length;
            }
          } catch { warn('historical backfill deferred; original DSH events remain intact'); }
          finally {
            try { await handle?.close(); }
            catch { warn('historical backfill handle close failed'); }
          }
        })();
        backfills.add(task);
        try { await task; } finally { backfills.delete(task); }
        void outbox.drain();
      });
      ctx.on('session/disposed', session => {
        try { outbox.enqueue('/schedule', { session_id: String(session.id) }); }
        catch { warn('local schedule write failed; raw dialogue remains in DSH persistence'); }
        void outbox.drain();
      });
      const retry = setInterval(() => { void outbox.drain(); }, Math.max(100, Number(config.retryMs) || 5000));
      retry.unref();
      void outbox.drain();
      ctx.on('dispose', async () => {
        closed = true;
        clearInterval(retry);
        await Promise.allSettled([...backfills]);
        await outbox.close();
      });
    } catch {
      warn('local outbox unavailable; capture disabled until plugin restart');
    }
  }

  ctx.on('agent/pre-step', async ({ agent, messages, signal }, next) => {
    const decision = await next();
    if (decision.kind !== 'enter' || signal?.aborted || !Array.isArray(decision.messages)) return decision;
    try {
      const history = agent.session.deriveMessages();
      const query = lastHumanMessage(decision.messages) || lastQueries.get(agent) || lastHumanMessage(history);
      if (!query) return decision;
      lastQueries.set(agent, query);
      const context = [...history, ...decision.messages].map(message => flatten(message.content))
        .filter(Boolean).join('\n');
      const data = await post(url, '/recall', {
        query, context, max_chars: maxChars, limit: config.limit ?? 5, depth: config.depth ?? 2,
        semantic_timeout_ms: Math.max(50, timeout - 300),
      }, timeout, token, signal);
      if (signal?.aborted || !data?.ok || !data.injection) return decision;
      const injected = {
        role: 'user', id: randomUUID(), content: [{ type: 'text', text: data.injection }],
        source: { kind: KIND, form: 'recall' },
      };
      const anchor = decision.messages.findLastIndex(message => messages.includes(message));
      const nextMessages = [...decision.messages];
      nextMessages.splice(anchor + 1, 0, injected);
      return { ...decision, messages: nextMessages };
    } catch {
      if (!signal?.aborted) warn('recall unavailable; conversation continues');
      return decision;
    }
  });
}
