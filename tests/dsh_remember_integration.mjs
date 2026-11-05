/** Real installed DSH lifecycle + HTTP + SQLite; scripted model is only a fixture. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import * as Goldfish from '../dsh-plugin/index.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const requireRuntime = createRequire(join(process.env.DSH_TEST_RUNTIME, 'package.json'));
const load = name => import(pathToFileURL(requireRuntime.resolve('@deepseek-ai/' + name)));
const [cordis, llm, sessions, projections, prompt, tools, agents, loop, jsonl] = await Promise.all([
  'cordis', 'dsh-llm', 'dsh-session', 'dsh-session-projection', 'dsh-system-prompt', 'dsh-tools', 'dsh-agent', 'dsh-agent-loop', 'dsh-session-persistence-jsonl',
].map(load));
const text = message => message.content.filter(block => block.type === 'text').map(block => block.text).join('');

test('B2 real DSH: remember→retry→search→evidence, deny negative and injected requests, no models', { timeout: 30000 }, async t => {
  const temp = await mkdtemp(join(tmpdir(), 'goldfish-remember-dsh-'));
  const database = join(temp, 'memory.sqlite3'), key = join(temp, 'remember.key');
  const reservation = createServer(); reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening'); const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const env = { ...process.env, PYTHONPATH: root, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' };
  for (const name of Object.keys(env)) if (name.startsWith('GOLDFISH_')) delete env[name];
  const backend = spawn(process.env.GOLDFISH_TEST_PYTHON || 'python', ['-m', 'goldfish.server', '--port', String(port),
    '--db', database, '--read-only', '--allow-remember', '--remember-key-file', key], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let diagnostic = '', ctx;
  backend.stdout.on('data', b => { diagnostic += b; }); backend.stderr.on('data', b => { diagnostic += b; });
  const url = `http://127.0.0.1:${port}`;
  try {
    for (let i = 0; ; i++) {
      try { if ((await fetch(url + '/health')).ok) break; } catch {}
      if (i > 100 || backend.exitCode !== null) throw Error('Backend startup: ' + diagnostic);
      await new Promise(resolve => setTimeout(resolve, 30));
    }
    assert.equal((await fetch(url + '/remember', { method: 'POST', body: '{}' })).status, 403);
    ctx = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) await ctx.plugin(plugin);
    await ctx.plugin(jsonl, { root: join(temp, 'sessions'), compression: 'none' });
    await ctx.plugin(loop.default, { agents: [] });
    const responses = [], seen = [];
    class Adapter extends llm.LlmAdapter {
      async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 100000 } }; }
      async *stream(options) {
        seen.push(options);
        const human = options.messages.findLast(m => m.source?.kind === 'user' || m.source?.kind === 'plugin:test');
        const input = text(human), start = options.messages.lastIndexOf(human);
        const results = options.messages.slice(start).filter(m => m.role === 'tool');
        const step = results.length;
        const denied = input.includes('不要') || human.source.kind !== 'user';
        if (step > 0) responses.push(JSON.parse(text(results.at(-1))));
        const count = input.startsWith('历史原话：') ? 0 : denied ? 1 : 4;
        if (step < count) {
          let name = 'goldfish_remember', args = { quote: 'B2真实宿主合成验收鱼R27', request_quote: '请记住' };
          if (input.includes('上一条')) args.request_quote = '请记住我上一条消息';
          if (step === 2) { name = 'goldfish_search'; args = { query: 'B2真实宿主合成验收鱼R27' }; }
          if (step === 3) { name = 'goldfish_evidence'; args = { memory_id: JSON.parse(text(results.at(-1))).records[0].id }; }
          yield { type: 'block-start', index: 0, blockType: 'tool-call' };
          yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: llm.ToolCallId('call-' + seen.length), name, arguments: JSON.stringify(args) } };
          yield { type: 'finish', reason: { kind: 'tool-calls' } }; return;
        }
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: '验收完成' } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    ctx.llm.registerAdapter(['synthetic'], new Adapter());
    await ctx.plugin(Goldfish, { serverUrl: url, enableRemember: true, autoCapture: false, rememberKeyFile: key });
    let agent = await ctx.agentLoop.create(sessions.SessionId('remember-test'), { provider: 'synthetic', model: 'fixture' });
    const send = async (value, kind = 'user') => {
      agent.followup(llm.createUserMessage({ source: { kind }, content: [{ type: 'text', text: value }] }));
      await agent.whenIdle();
    };
    await send('请记住：B2真实宿主合成验收鱼R27。');
    assert.equal(responses.length, 4);
    assert.equal(responses[0].saved, true, JSON.stringify(responses[0]));
    assert.equal(responses[1].duplicate, true);
    assert.equal(responses[0].receipt_id, responses[1].receipt_id);
    assert.equal(responses[2].records[0].kind, 'explicit_user_statement');
    assert(responses[3].records.some(row => row.text.includes('请记住：B2真实宿主合成验收鱼R27')));
    const before = await (await fetch(url + '/stats')).json();
    await send('不要保存。请记住：B2真实宿主合成验收鱼R27。');
    assert.equal(responses.at(-1).saved, false);
    await send('请记住：B2真实宿主合成验收鱼R27。', 'plugin:test');
    assert.equal(responses.at(-1).saved, false);
    assert.deepEqual(await (await fetch(url + '/stats')).json(), before);
    assert(seen.every(request => !request.messages.some(m => m.source?.kind === 'plugin:goldfish-memory')));
    const health = await (await fetch(url + '/health')).json();
    assert(health.explicit_remember && health.query_only);
    agent = await ctx.agentLoop.create(sessions.SessionId('historical-source'), { provider: 'synthetic', model: 'fixture' });
    await send('历史原话：B2真实宿主合成验收鱼R27');
    await ctx.fiber.dispose(); ctx = undefined;
    ctx = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) await ctx.plugin(plugin);
    await ctx.plugin(jsonl, { root: join(temp, 'sessions'), compression: 'none' });
    await ctx.plugin(loop.default, { agents: [] });
    ctx.llm.registerAdapter(['synthetic'], new Adapter());
    await ctx.plugin(Goldfish, { serverUrl: url, enableRemember: true, autoCapture: false, rememberKeyFile: key });
    const restored = await ctx.agentLoop.resume(ctx, { resumeSessionId: sessions.SessionId('historical-source'), agentOptions: { provider: 'synthetic', model: 'fixture' } });
    agent = restored.agent;
    responses.length = 0;
    await send('请记住我上一条消息。');
    assert.equal(responses[0].saved, true, JSON.stringify(responses[0]));
    assert.equal(responses[0].duplicate, false, 'same wording from a different real event retains its own provenance');
    assert.equal(responses[1].replayed_receipt, true);
    await restored.dispose();
    t.diagnostic('Real DSH lifecycle and tool identities accepted; duplicate retry returned same receipt; negation/injection denied; 0 automatic injections and 0 background model calls.');
    t.diagnostic('JSONL restore: a new explicit request located the immediately preceding original user event via public paged persistence; no old consent was reused.');
  } finally {
    await ctx?.fiber.dispose();
    if (backend.exitCode === null) { const ended = once(backend, 'exit'); backend.kill(); await ended; }
    await rm(temp, { recursive: true, force: true });
  }
});
