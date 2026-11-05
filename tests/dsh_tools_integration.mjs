/** Real installed DSH + Python HTTP/SQLite, synthetic Agent adapter. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import * as Goldfish from '../dsh-plugin/index.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const requireRuntime = createRequire(join(process.env.DSH_TEST_RUNTIME, 'package.json'));
const load = name => import(pathToFileURL(requireRuntime.resolve('@deepseek-ai/' + name)));
const [cordis, llm, sessions, projections, prompt, tools, agents, loop] = await Promise.all([
  'cordis', 'dsh-llm', 'dsh-session', 'dsh-session-projection', 'dsh-system-prompt', 'dsh-tools', 'dsh-agent', 'dsh-agent-loop',
].map(load));
const text = message => message.content.filter(block => block.type === 'text').map(block => block.text).join('');

test('tools mode: optional lookup, search→explore→evidence, real HTTP, no injection, offline reply', { timeout: 30000 }, async t => {
  const temp = await mkdtemp(join(tmpdir(), 'goldfish-tools-dsh-'));
  const database = join(temp, 'memory.sqlite3');
  const python = process.env.GOLDFISH_TEST_PYTHON || 'python';
  const env = { ...process.env, PYTHONPATH: root, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' };
  for (const key of Object.keys(env)) if (key.startsWith('GOLDFISH_')) delete env[key];
  execFileSync(python, ['-c', `from goldfish.engine import MemoryDB
import sys
d=MemoryDB(sys.argv[1])
a=d.add_message('seed','user','2025年我把编号RS-17的红伞借给小林。',message_id='seed:1')
b=d.add_message('seed','user','小林当天坐火车去杭州，这把伞是为出行借的。',message_id='seed:2')
x=d.add_summary('红伞RS-17借给小林',[a],summary_id='umbrella')
y=d.add_summary('小林坐火车去杭州',[b],summary_id='journey')
d.add_link(x,y,'same_event','同一次出行的借伞与交通安排',origin='human_review',confirmed=True)
d.close()`, database], { cwd: root, env });
  const reservation = createServer();
  reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening');
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  let backend = spawn(python, ['-m', 'goldfish.server', '--port', String(port), '--db', database], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  backend.stderr.on('data', data => { output += data; });
  backend.stdout.on('data', data => { output += data; });
  let ctx;
  try {
    for (let i = 0; ; i++) {
      try { if ((await fetch(`http://127.0.0.1:${port}/health`)).ok) break; } catch {}
      if (i > 100 || backend.exitCode !== null) throw Error('Fixture backend failed: ' + output);
      await new Promise(resolve => setTimeout(resolve, 30));
    }
    ctx = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) await ctx.plugin(plugin);
    await ctx.plugin(loop.default, { agents: [] });
    const seen = [];
    class Adapter extends llm.LlmAdapter {
      step = 0;
      async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 100000 } }; }
      async *stream(options) {
        seen.push(options);
        const latest = options.messages.findLast(message => message.source?.kind === 'user');
        const query = text(latest);
        const results = options.messages.filter(message => message.role === 'tool');
        if (query === '无需历史的普通问题') {
          yield { type: 'block-start', index: 0, blockType: 'text' };
          yield { type: 'block-end', index: 0, block: { type: 'text', text: '直接回答' } };
          yield { type: 'finish', reason: { kind: 'stop' } };
          return;
        }
        if (this.step < (query === '服务关闭后查询' ? 1 : 3)) {
          let name, args;
          if (this.step === 0) { name = 'goldfish_search'; args = { query: 'RS-17' }; }
          else {
            const parsed = JSON.parse(text(results.at(-1)));
            assert(parsed.ok && parsed.records.length);
            if (this.step === 1) { name = 'goldfish_explore'; args = { memory_ids: [parsed.records[0].memory_ref], include_candidates: false }; }
            else { name = 'goldfish_evidence'; args = { memory_id: parsed.records[0].memory_ref }; }
          }
          yield { type: 'block-start', index: 0, blockType: 'tool-call' };
          yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: llm.ToolCallId('call-' + this.step++), name, arguments: JSON.stringify(args) } };
          yield { type: 'finish', reason: { kind: 'tool-calls' } };
          return;
        }
        const last = JSON.parse(text(results.at(-1)));
        if (query === '服务关闭后查询') assert.equal(last.ok, false);
        else assert(last.records.some(row => row.text.includes('火车去杭州')));
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: query === '服务关闭后查询' ? '记忆不可用，仍能回复' : '小林，坐火车去杭州。' } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    const adapter = new Adapter();
    ctx.llm.registerAdapter(['synthetic'], adapter);
    await ctx.plugin(Goldfish, { serverUrl: `http://127.0.0.1:${port}`, autoCapture: false, timeoutMs: 500 });
    const agent = await ctx.agentLoop.create(sessions.SessionId('tools-test'), { provider: 'synthetic', model: 'fixture' });
    const send = async value => {
      agent.followup(llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: value }] }));
      await agent.whenIdle();
    };
    await send('无需历史的普通问题');
    assert.equal(seen.length, 1);
    assert.equal(adapter.step, 0);
    const before = await (await fetch(`http://127.0.0.1:${port}/stats`)).json();
    await send('回忆并查看出行背景与原话');
    assert.equal(adapter.step, 3);
    assert.equal(seen.length, 5);
    assert(seen.every(request => !request.messages.some(message => message.source?.kind === 'plugin:goldfish-memory')));
    const after = await (await fetch(`http://127.0.0.1:${port}/stats`)).json();
    assert.deepEqual(after, before, 'three retrieval tools must not change SQLite memory counts');
    adapter.step = 0;
    const exited = once(backend, 'exit');
    backend.kill();
    await exited;
    backend = null;
    await send('服务关闭后查询');
    assert.equal(text(agent.session.deriveMessages().at(-1)), '记忆不可用，仍能回复');
    t.diagnostic('Real DSH 0.2.1-rc.5 + Python HTTP/SQLite: 0 optional calls, 3 linked tool calls, 0 auto injections, read-only counts unchanged, offline final reply succeeds. Agent model is a synthetic fixture.');
  } finally {
    await ctx?.fiber.dispose();
    if (backend && backend.exitCode === null) { const exited = once(backend, 'exit'); backend.kill(); await exited; }
    await rm(temp, { recursive: true, force: true });
  }
});
