/** Host-owned explicit-write authority. Model arguments never supply identities. */
import { createHash, createHmac } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { homedir } from 'node:os';

const optional = (ctx, name) => typeof ctx.get === 'function' ? ctx.get(name) : ctx[name];
const flatten = value => typeof value === 'string' ? value : (value || [])
  .filter(block => block?.type === 'text').map(block => block.text || '').join('');
export function unquoted(text) {
  return text.replace(/```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|^\s*>[^\n]*/gm,
    match => ' '.repeat(match.length));
}
export function explicitRequest(text, request) {
  if (typeof request !== 'string' || !request.trim() || request.length > 2000) return false;
  const plain = unquoted(text);
  if (!plain.includes(request)) return false;
  if (/(?:不要|不用|无需|不必|禁止|别)\s*(?:再|实际|真的|自动|帮我|替我|给我|把它|把这条|把这个|进行|做)?\s*(?:记住|记下|记忆|保存|存|写入|记录)|示例|例如|举例|演示|记住了?[吗么没]|记住什么|\bexample\b|(?:do\s+not|don.t|never)\s+(?:actually\s+)?(?:remember|save|store|record)/i.test(plain)) return false;
  const directive = /(?:^|[。！？!?；;\n])\s*(?:(?:请你|请|麻烦|帮我|替我|现在|务必|我希望你)\s*)*(?:记住|记下|记到长期记忆|保存为记忆|存入长期记忆)|(?:^|[.!?;\n])\s*(?:please\s+)?(?:remember\b|save\b.{0,40}\bmemory\b)/gi;
  return [...plain.matchAll(directive)].some(match => {
    const at = plain.indexOf(request, match.index);
    return at >= match.index && at < match.index + match[0].length;
  });
}

export function createRemember(ctx, config, request) {
  const turns = new WeakMap();
  const recent = new WeakMap();
  const home = optional(ctx, 'pluginPackages')?.resolution?.profileDir || process.env.DSH_HOME || join(homedir(), '.dsh');
  const namespace = createHash('sha256').update(home).digest('hex').slice(0, 24);
  const keyFile = config.rememberKeyFile || join(process.env.LOCALAPPDATA || join(homedir(), 'AppData/Local'), 'GoldfishMemory/remember-write.key');
  ctx.on('session/event', (session, event) => {
    if (event.seq < (session.inheritedEventCount ?? 0)) return;
    if (event.type === 'turn/start') turns.set(session, { start: event.seq });
    if (event.type === 'turn/end') turns.delete(session);
    if (event.type !== 'user/message' || event.data?.source?.kind !== 'user') return;
    const list = recent.get(session) || [];
    list.push(event);
    if (list.length > 128) list.shift();
    recent.set(session, list);
    const turn = turns.get(session);
    if (turn) turn.latest = event;
  });
  ctx.on('session/disposed', session => { turns.delete(session); recent.delete(session); });

  async function locate(session, message) {
    const matches = event => event.type === 'user/message' && event.data?.source?.kind === 'user'
      && event.data.id === message.id && flatten(event.data.content) === flatten(message.content);
    const cached = (recent.get(session) || []).findLast(matches);
    if (cached) return cached;
    const persistence = optional(ctx, 'sessionPersistence');
    if (!persistence) throw Error('source_not_locatable');
    let handle;
    try {
      handle = await persistence.open(session.id, 'read');
      let offset = handle.inheritedEventCount ?? 0;
      const end = session.firstLiveSeq ?? session.seq;
      while (offset < end) {
        const { events } = await handle.read(offset, Math.min(256, end - offset));
        if (!events.length) break;
        const found = events.find(matches);
        if (found) return found;
        offset += events.length;
      }
      throw Error('source_not_locatable');
    } finally { await handle?.close(); }
  }
  const source = (session, event) => ({ namespace, session_id: String(session.id), event_seq: event.seq,
    message_id: String(event.data.id), event_time: new Date(event.time).toISOString(), role: 'user',
    source_kind: 'user', text: flatten(event.data.content) });

  return async (args, exec) => {
    let submitted = false;
    try {
      const agent = exec.agent, registry = optional(ctx, 'agents');
      if (!agent || !registry || registry.get(agent.id) !== agent || registry.currentInitiator() !== agent
          || agent.status !== 'running' || !registry.roots().includes(agent)) throw Error('live_root_agent_required');
      const session = agent.session, turn = turns.get(session), auth = turn?.latest;
      if (!auth || auth.seq <= turn.start) throw Error('current_human_request_required');
      const quote = args.quote, requestQuote = args.request_quote;
      if (typeof quote !== 'string' || !quote.trim() || quote !== quote.trim() || quote.length > 2000
          || !explicitRequest(flatten(auth.data.content), requestQuote)) throw Error('explicit_literal_request_required');
      const humans = session.deriveMessages().filter(m => m.role === 'user' && m.source?.kind === 'user');
      if (humans.at(-1)?.id !== auth.data.id) throw Error('current_source_changed');
      // A simple “remember the previous message” can cite the immediately
      // preceding human message. Arbitrary history needs a fresh literal quote.
      let message = humans.at(-1);
      if (!flatten(message.content).includes(quote)) {
        if (!/(?:刚才|上一(?:条|句)|前一(?:条|句))|\b(?:previous|last)\s+(?:message|statement)\b/i.test(requestQuote)) throw Error('repeat_quote_in_current_request');
        message = humans.at(-2);
        if (!message || !flatten(message.content).includes(quote)) throw Error('literal_user_source_required');
      }
      const target = await locate(session, message);
      const key = await readFile(keyFile);
      if (key.length !== 32) throw Error('write_capability_unavailable');
      // Authority can change while persistence/key I/O is pending.
      if (turns.get(session) !== turn || turn.latest !== auth || agent.status !== 'running' || exec.signal?.aborted)
        throw Error('request_cancelled_or_changed');
      const payload = { version: 1, issued_at: Date.now() / 1000, quote, request_quote: requestQuote,
        authorization: source(session, auth), source: source(session, target),
        previous_user_message_id: humans.at(-2)?.id || null };
      const signature = createHmac('sha256', key).update(JSON.stringify(payload)).digest('hex');
      submitted = true;
      const result = await request('/remember', payload, exec.signal, { 'X-Goldfish-Remember-Signature': signature });
      if (!result || result.ok !== true || result.saved !== true || typeof result.memory_id !== 'string'
          || typeof result.receipt_id !== 'string') throw Error('invalid_write_response');
      return JSON.stringify(result);
    } catch (error) {
      const rejected = !submitted || [400, 401, 403, 404, 413].includes(error.status);
      const reasons = new Set(['live_root_agent_required', 'current_human_request_required', 'explicit_literal_request_required',
        'current_source_changed', 'repeat_quote_in_current_request', 'literal_user_source_required', 'source_not_locatable',
        'write_capability_unavailable', 'request_cancelled_or_changed']);
      return JSON.stringify({ ok: false, saved: rejected ? false : 'unknown',
        error: rejected ? 'explicit_request_or_source_not_authorized' : 'write_result_unknown',
        reason: !submitted && reasons.has(error.message) ? error.message : undefined,
        hint: rejected ? 'Only save a literal quote from the latest real user request, or the immediately preceding user message explicitly referenced by it. Ask the user to repeat the quote if needed. Do not claim it was saved.'
          : 'The response was interrupted; a commit may already exist. Retry the same quote and request to obtain its durable receipt. Do not claim success or definite failure.' });
    }
  };
}
