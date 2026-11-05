/** Goldfish: optional DSH memory tools; legacy automatic recall stays opt-in. */
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { Outbox } from './outbox.js';
import { registerTools } from './tools.js';
import { createRemember } from './remember.js';
import { registerGuide } from './guide.js';
import { registerCompaction } from './compaction.js';

export const name = 'goldfish-memory';
export const inject = ['agents', 'tools'];
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
    if (text) return message;
  }
  return undefined;
}

async function post(url, path, payload, timeoutMs, token, signal, headers = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url + path, {
      method: 'POST', redirect: 'error',
      headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Goldfish-Token': token } : {}), ...headers },
      body: JSON.stringify(payload),
      signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
    });
    if (!response.ok) {
      const detail=path==='/compaction'?await response.json().catch(()=>({})):{};
      throw Object.assign(new Error('HTTP ' + response.status), {status:response.status,validationCode:detail.validation_code});
    }
    return await response.json();
  } finally { clearTimeout(timer); }
}

export function apply(ctx, config = {}) {
  const mode = config.mode ?? 'tools';
  if (!['tools', 'legacy_auto', 'off'].includes(mode)) throw new Error('Invalid Goldfish mode');
  if (mode === 'off') return;
  const url = String(config.serverUrl ?? 'http://127.0.0.1:18320').replace(/\/$/, '');
  const endpoint = new URL(url);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password
      || endpoint.search || endpoint.hash) throw new Error('Invalid Goldfish server URL');
  if (endpoint.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)) {
    throw new Error('Remote Goldfish servers require HTTPS');
  }
  const token = config.token ?? process.env.GOLDFISH_LOCAL_TOKEN ?? '';
  const timeout = Math.max(50, Math.min(Number(config.timeoutMs) || 12000, 30000));
  const maxChars = Math.max(250, Math.min(Number(config.maxChars) || 1800, 8000));
  const warn = message => ctx.logger?.warn('[goldfish] ' + message);
  // A genuine new input gets one recall attempt. Tool continuations never ask
  // the LLM to reconsider the same question. Keep only this turn's result so
  // compaction can restore it without another backend/model call.
  const recalls = new WeakMap();

  if (config.autoCapture === true || (mode === 'legacy_auto' && config.autoCapture !== false)) {
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
        if (event.type === 'turn/end') {
          outbox.enqueue('/schedule', { session_id: String(session.id), event_seq: event.seq });
          return;
        }
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
          consolidate_after: false,
        }, { deferUntilSchedule: true });
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
        try { outbox.enqueue('/schedule', { session_id: String(session.id),
          event_seq: Number.isSafeInteger(session.seq) ? session.seq - 1 : Number.MAX_SAFE_INTEGER }); }
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

  if (mode === 'tools') {
    const request = (path, payload, signal, headers) => post(url, path, payload, timeout, token, signal, headers);
    const remember = config.enableRemember === true ? createRemember(ctx, config, request) : undefined;
    return (async () => {
      await registerTools(ctx, request, remember);
      if (config.enableGuide === true) {
        try {
          await ctx.plugin({ name: 'goldfish-session-guide',
            inject: ['agents', 'tools', 'systemPrompt', 'sessionProjections'], apply: registerGuide });
        } catch {
          // An optional session feature must not take down the DSH host or B1/B2.
          warn('session guide unavailable; B1/B2 tools remain available. Check native DSH projection/prompt dependencies.');
        }
      }
      if (config.enableCompaction === true) {
        try {
          await ctx.plugin({ name: 'goldfish-compaction-handoff',
            inject: ['llm', 'sessions', 'sessionPersistence', 'tools'],
            apply: child => registerCompaction(child, config, request) });
        } catch {
          warn('compaction handoff unavailable; B1/B2/C remain available. Original native history is preserved.');
        }
      }
    })();
  }
  ctx.on('agent/pre-step', async ({ agent, messages, signal, turn }, next) => {
    const decision = await next();
    if (decision.kind !== 'enter' || signal?.aborted || !Array.isArray(decision.messages)) return decision;
    try {
      const history = agent.session.deriveMessages();
      const human = lastHumanMessage(decision.messages);
      const previous = recalls.get(agent.session);
      const identity = human?.id ?? human;
      const fresh = human && identity !== previous?.identity;
      if (!fresh) {
        // The checkpoint replaced this turn's recalled context. Re-admit the
        // cached message once; normal tool steps retain the original history.
        const compacted = history.some(message => ['compact-checkpoint', 'compact'].includes(message.source?.kind));
        if (previous?.injected && previous.turn === turn && compacted
            && !history.some(message => message.source?.kind === KIND)
            && !decision.messages.some(message => message.source?.kind === KIND)) {
          return { ...decision, messages: [...decision.messages, previous.injected] };
        }
        return decision;
      }
      const state = { identity, turn };
      recalls.set(agent.session, state);
      const query = flatten(human.content).trim();
      const context = [...history, ...decision.messages].map(message => flatten(message.content))
        .filter(Boolean).join('\n');
      const data = await post(url, '/recall', {
        query, context, max_chars: maxChars, limit: config.limit ?? 5, depth: config.depth ?? 0,
        semantic_timeout_ms: Math.max(50, timeout - 300),
        semantic_budget_ms: Math.max(100, timeout - 1500),
      }, timeout, token, signal);
      if (signal?.aborted || !data?.ok || !data.injection) return decision;
      const injected = {
        role: 'user', id: randomUUID(), content: [{ type: 'text', text: data.injection }],
        source: { kind: KIND, form: 'recall' },
      };
      state.injected = injected;
      const anchor = decision.messages.findLastIndex(message => messages.includes(message));
      const nextMessages = [...decision.messages];
      nextMessages.splice(anchor + 1, 0, injected);
      return { ...decision, messages: nextMessages };
    } catch {
      if (!signal?.aborted) warn('recall unavailable; conversation continues');
      return decision;
    }
  });
  ctx.logger?.info?.('[goldfish] ready: recall once per human input; consolidate after turn/end');
}
