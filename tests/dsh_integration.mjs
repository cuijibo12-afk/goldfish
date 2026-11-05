/**
 * Real DSH 0.2.1-rc.5 host, real Goldfish HTTP/SQLite, synthetic local models.
 * Install the pinned runtime outside the project, then run:
 * DSH_TEST_RUNTIME=/path/to/runtime node --test tests/dsh_integration.mjs
 * No paid API, personal profile, or real conversation is used.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { mkdtemp, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import * as Goldfish from '../dsh-plugin/index.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const runtime = process.env.DSH_TEST_RUNTIME;
if (!runtime) throw new Error('Set DSH_TEST_RUNTIME to a directory containing @deepseek-ai/dsh@0.2.1-rc.5');
const requireRuntime = createRequire(join(resolve(runtime), 'package.json'));
const load = name => import(pathToFileURL(requireRuntime.resolve(name)));
const version = requireRuntime('@deepseek-ai/dsh/package.json').version;
assert.equal(version, '0.2.1-rc.5', 'This integration contract is pinned to DSH 0.2.1-rc.5');
const [cordis, llm, sessions, projections, prompt, tools, agents, loop, jsonl, meter, compact] = await Promise.all([
  '@deepseek-ai/cordis', '@deepseek-ai/dsh-llm', '@deepseek-ai/dsh-session',
  '@deepseek-ai/dsh-session-projection', '@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-agent', '@deepseek-ai/dsh-agent-loop', '@deepseek-ai/dsh-session-persistence-jsonl',
  '@deepseek-ai/dsh-token-meter', '@deepseek-ai/dsh-compaction-basic',
].map(load));
const flatten = message => message.content.filter(block => block.type === 'text').map(block => block.text).join('');
const memoryMessages = messages => messages.filter(message => message.source.kind === 'plugin:goldfish-memory');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, description, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check()) return;
    await pause(25);
  }
  throw new Error('Timed out: ' + description);
}
async function listen(server, port = 0) {
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}
async function closeServer(server) {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}

class ScriptedAdapter extends llm.LlmAdapter {
  requests = [];
  failNext = false;
  toolNext = false;
  toolStepsRemaining = 0;
  async resolveModel(provider, model) {
    return { provider, id: model, name: model, context: { contextWindow: 100000 } };
  }
  async *stream(options) {
    this.requests.push(options);
    if (this.failNext) {
      this.failNext = false;
      yield { type: 'finish', reason: { kind: 'error', failure: { message: 'synthetic retry', code: 'SERVER_ERROR' } } };
      return;
    }
    if (this.toolNext || this.toolStepsRemaining > 0) {
      this.toolNext = false;
      if (this.toolStepsRemaining > 0) {
        this.toolStepsRemaining--;
        yield { type: 'block-start', index: 1, blockType: 'text' };
        yield { type: 'block-end', index: 1, block: { type: 'text', text: '中间执行进展' } };
      }
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: llm.ToolCallId('synthetic-tool-call'), name: 'goldfish_test_echo', arguments: '{}' } };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
      return;
    }
    const text = '合成测试回复';
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text };
    yield { type: 'block-end', index: 0, block: { type: 'text', text } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

class ControlledCompaction extends compact.BasicCompactionEngine {
  checkpoint = '讨论了一段往事；具体饮品细节已省略。';
  async summarize() {
    return { summary: [{ type: 'text', text: this.checkpoint }], provider: 'synthetic', model: 'synthetic' };
  }
}

test('pinned DSH host: capture, five turns, actual compaction, cancellation, retry, outage, resume, automatic consolidation', { timeout: 90000 }, async t => {
  const temp = await mkdtemp(join(tmpdir(), 'goldfish-dsh-'));
  const db = join(temp, 'memory.sqlite3');
  const outboxDir = join(temp, 'outbox');
  const sessionRoot = join(temp, 'dsh-sessions');
  const fact = '用户在旧码头喜欢喝桂花拿铁';
  let backend;
  let backendOutput = '';
  let ctx;
  let bridge;
  let model;
  let backendPort;
  let holdRecall;
  const recallCalls = [];
  const deliveries = [];
  const modelInputs = [];
  const python = process.env.GOLDFISH_TEST_PYTHON || 'python';
  const runPython = (code, args = []) => execFileSync(python, ['-c', code, ...args], { cwd: root, encoding: 'utf8' });
  const rows = (sql, params = []) => JSON.parse(runPython(
    'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); c.row_factory=sqlite3.Row; print(json.dumps([dict(x) for x in c.execute(sys.argv[2],json.loads(sys.argv[3]))],ensure_ascii=False))',
    [db, sql, JSON.stringify(params)]));
  async function stopBackend() {
    if (!backend || backend.exitCode !== null) return;
    const stopped = once(backend, 'exit');
    backend.kill('SIGTERM');
    await stopped;
  }
  async function startBackend(modelPort) {
    const env = { ...process.env, PYTHONPATH: root };
    for (const name of Object.keys(env)) if (name.startsWith('GOLDFISH_')) delete env[name];
    Object.assign(env, { GOLDFISH_LLM_PROVIDER: 'ollama', GOLDFISH_LLM_MODEL: 'synthetic',
      GOLDFISH_LLM_URL: `http://127.0.0.1:${modelPort}` });
    backend = spawn(python, ['-m', 'goldfish.server', '--port', String(backendPort), '--db', db], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
    backend.stdout.on('data', data => { backendOutput += data; });
    backend.stderr.on('data', data => { backendOutput += data; });
    await until(async () => {
      if (backend.exitCode !== null) throw new Error('Backend failed: ' + backendOutput);
      try { return (await fetch(`http://127.0.0.1:${backendPort}/health`)).ok; } catch { return false; }
    }, 'Goldfish backend startup');
  }
  async function mount(serverUrl, capture = true) {
    const context = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) {
      await context.plugin(plugin);
    }
    await context.plugin(jsonl, { root: sessionRoot, compression: 'none' });
    await context.plugin(loop.default, { agents: [] });
    await context.plugin(meter.default);
    const adapter = new ScriptedAdapter();
    context.llm.registerAdapter(['synthetic'], adapter);
    await context.plugin(Goldfish, { mode: 'legacy_auto', serverUrl, outboxDir, retryMs: 100, timeoutMs: 500, autoCapture: capture });
    return { context, adapter };
  }
  const send = async (agent, text) => {
    agent.followup(llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] }));
    await agent.whenIdle();
  };
  try {
    runPython('from goldfish.engine import MemoryDB; import sys; d=MemoryDB(sys.argv[1]); m=d.add_message("seed","user",sys.argv[2]); d.add_summary(sys.argv[2],[m]); d.close()', [db, fact]);
    const reservation = createServer();
    backendPort = await listen(reservation);
    await closeServer(reservation);
    model = createServer(async (request, response) => {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const payload = JSON.parse(Buffer.concat(chunks));
      const input = JSON.parse(payload.messages[1].content);
      modelInputs.push(input);
      const source = input.raw_messages.find(message => message.role === 'user' && message.text.includes('自动整理样本'));
      const result = { summaries: source ? [{ ref: 'n1', text: '用户曾在山顶看流星雨', evidence_ids: [source.id], theme: '观星经历' }] : [], profiles: [], links: [] };
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ message: { content: JSON.stringify(result) } }));
    });
    const modelPort = await listen(model);
    await startBackend(modelPort);
    bridge = createServer(async (request, response) => {
      try {
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        deliveries.push({ path: request.url, payload: JSON.parse(body) });
        if (request.url === '/recall') {
          recallCalls.push(JSON.parse(body));
          if (holdRecall) await holdRecall.promise;
        }
        const upstream = await fetch(`http://127.0.0.1:${backendPort}${request.url}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
        response.writeHead(upstream.status, { 'Content-Type': 'application/json' });
        response.end(await upstream.text());
      } catch { if (!response.destroyed) { response.writeHead(503); response.end('{}'); } }
    });
    const serverUrl = `http://127.0.0.1:${await listen(bridge)}`;
    let mounted = await mount(serverUrl);
    ctx = mounted.context;
    let adapter = mounted.adapter;
    const agent = await ctx.agentLoop.create(sessions.SessionId('five-turns'), { provider: 'synthetic', model: 'synthetic' });
    for (let turn = 0; turn < 5; turn++) await send(agent, `旧码头的桂花拿铁，第${turn + 1}轮。${'这是合成的往事测试文本。'.repeat(40)}`);
    assert.equal(adapter.requests.length, 5);
    assert(adapter.requests.every(request => memoryMessages(request.messages).length === 1), 'Only one durable memory message across five model inputs');
    assert.equal(agent.session.snapshotEvents().filter(event => event.type === 'user/message' && event.data.source.kind === 'plugin:goldfish-memory').length, 1);
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['five-turns']).length === 10, 'human and assistant capture');
    assert(rows('SELECT * FROM raw_messages WHERE session_id=?', ['five-turns']).every(row => !row.text.includes('<goldfish-memory>')));
    t.diagnostic('PASS: five actual host turns; one durable injection; ten real event captures; no injection pollution');

    const engine = new ControlledCompaction(ctx, { auto: false });
    assert(await engine.compactNow(agent, new AbortController().signal), 'Official compaction engine must perform a useful compaction');
    assert.equal(memoryMessages(agent.session.deriveMessages()).length, 0, 'Official compaction removed the old memory from effective context');
    assert(!agent.session.deriveMessages().some(message => flatten(message).includes(fact)));
    await send(agent, '旧码头的桂花拿铁是什么？');
    assert.equal(memoryMessages(adapter.requests.at(-1).messages).length, 1);
    engine.checkpoint = fact;
    assert(await engine.compactNow(agent, new AbortController().signal));
    const injectionCount = agent.session.snapshotEvents().filter(event => event.type === 'user/message' && event.data.source.kind === 'plugin:goldfish-memory').length;
    await send(agent, '继续说旧码头的桂花拿铁');
    assert.equal(agent.session.snapshotEvents().filter(event => event.type === 'user/message' && event.data.source.kind === 'plugin:goldfish-memory').length, injectionCount);
    t.diagnostic('PASS: official DSH compactNow replaces history; missing detail recalled again; retained verbatim fact suppresses injection');

    let toolExecutions = 0;
    const removeTool = ctx.tools.register(tools.defineContentToolFixture({
      name: 'goldfish_test_echo', description: 'Synthetic integration fixture', parameters: {},
      execute() { toolExecutions++; return [{ type: 'text', text: '合成工具执行完成' }]; },
    }));
    const toolAgent = await ctx.agentLoop.create(sessions.SessionId('tool-compaction'), { provider: 'synthetic', model: 'synthetic' });
    engine.checkpoint = '调用了测试工具；往事的具体细节已省略。';
    let compactedDuringToolTurn = false;
    const removeToolCompaction = ctx.on('agent/pre-step', async ({ agent: current, step, messages, signal }, next) => {
      if (current === toolAgent && step === 2) {
        assert(!messages.some(message => message.source.kind === 'user'), 'Tool continuation has no new human batch');
        const nodes = [...current.session.surface.nodes];
        const nonSystem = nodes.filter(seq => current.session.eventAt(seq).type !== 'system/message');
        await engine.compactRegion(nonSystem[0], nonSystem.at(-1), current, signal);
        assert(!current.session.deriveMessages().some(message => message.source.kind === 'user'), 'Official same-turn compaction removed all human query messages');
        assert.equal(memoryMessages(current.session.deriveMessages()).length, 0);
        compactedDuringToolTurn = true;
      }
      return next();
    });
    const requestsBeforeTool = adapter.requests.length;
    const recallsBeforeTool = recallCalls.length;
    adapter.toolNext = true;
    await send(toolAgent, `旧码头的桂花拿铁是什么？${'这是用于压缩的合成背景。'.repeat(40)}`);
    removeToolCompaction();
    removeTool();
    assert.equal(toolExecutions, 1, 'The host must execute the requested tool');
    assert(compactedDuringToolTurn);
    assert.equal(adapter.requests.length, requestsBeforeTool + 2, 'One model step before and one after the actual tool');
    assert.equal(recallCalls.length, recallsBeforeTool + 1, 'Compaction and tool continuation reuse the one recall result');
    assert.equal(memoryMessages(adapter.requests.at(-1).messages).length, 1, 'Recall must run after same-turn compaction even when no human query remains on the surface');
    const toolLog = toolAgent.session.snapshotEvents();
    assert.equal(toolLog.filter(event => event.type === 'turn/start').length, 1);
    assert.equal(toolLog.filter(event => event.type === 'user/message' && event.data.source.kind === 'plugin:goldfish-memory').length, 2);
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['tool-compaction']).length === 2, 'tool continuation source capture');
    t.diagnostic('PASS: actual tool and compactRegion restore cached memory with only one recall request');

    const manySteps = await ctx.agentLoop.create(sessions.SessionId('many-tool-steps'), { provider: 'synthetic', model: 'synthetic' });
    let executed = 0;
    const removeManyTool = ctx.tools.register(tools.defineContentToolFixture({
      name: 'goldfish_test_echo', description: 'Many-step turn fixture', parameters: {},
      execute() {
        executed++;
        assert.equal(deliveries.filter(item => item.payload.session_id === 'many-tool-steps').length, 0,
          'No partial turn is exposed to background consolidation');
        return [{ type: 'text', text: '工具结果' }];
      },
    }));
    const recallsBeforeMany = recallCalls.length;
    adapter.toolStepsRemaining = 12;
    await send(manySteps, '执行十二次合成工具操作，最后回复');
    removeManyTool();
    assert.equal(executed, 12);
    assert.equal(recallCalls.length, recallsBeforeMany + 1);
    await until(() => deliveries.some(item => item.path === '/schedule' && item.payload.session_id === 'many-tool-steps'), 'one completed-turn schedule');
    const turnDeliveries = deliveries.filter(item => item.payload.session_id === 'many-tool-steps');
    assert.equal(turnDeliveries.filter(item => item.path === '/schedule').length, 1);
    assert.equal(turnDeliveries.filter(item => item.path === '/ingest').length, 14);
    assert(turnDeliveries.filter(item => item.path === '/ingest').every(item => !item.payload.consolidate_after));
    t.diagnostic('PASS: twelve real host tool steps, one recall, no mid-turn deliveries, one final consolidation schedule');

    const cancelled = await ctx.agentLoop.create(sessions.SessionId('cancelled'), { provider: 'synthetic', model: 'synthetic' });
    holdRecall = Promise.withResolvers();
    const beforeRecall = recallCalls.length;
    cancelled.followup(llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '旧码头桂花拿铁取消测试' }] }));
    await until(() => recallCalls.length > beforeRecall, 'pre-step recall enters HTTP request');
    cancelled.cancel({ kind: 'user' });
    await cancelled.whenIdle();
    holdRecall.resolve();
    holdRecall = undefined;
    assert(!cancelled.session.snapshotEvents().some(event => event.type === 'user/message'), 'Aborted pre-step commits neither human input nor memory');
    await send(cancelled, '旧码头桂花拿铁重新发送');
    assert.equal(memoryMessages(adapter.requests.at(-1).messages).length, 1, 'Cancelled recall must not blacklist later injection');
    t.diagnostic('PASS: cancellation during actual pre-step HTTP recall leaves no committed input; next turn can inject');

    const retryAgent = await ctx.agentLoop.create(sessions.SessionId('retry'), { provider: 'synthetic', model: 'synthetic' });
    adapter.failNext = true;
    const removeRetry = ctx.on('agent/request-error', async () => ({ kind: 'retry' }));
    await send(retryAgent, '旧码头桂花拿铁重试');
    removeRetry();
    const retryEvents = retryAgent.session.snapshotEvents();
    assert.equal(retryEvents.filter(event => event.type === 'assistant/attempt').length, 1);
    assert.equal(retryEvents.filter(event => event.type === 'user/message' && event.data.source.kind === 'plugin:goldfish-memory').length, 1);
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['retry']).length === 2, 'retry capture');
    t.diagnostic('PASS: actual DSH request retry persists one user, one successful assistant, and one memory; failed attempt excluded');

    await stopBackend();
    const offline = await ctx.agentLoop.create(sessions.SessionId('offline'), { provider: 'synthetic', model: 'synthetic' });
    await send(offline, '记忆服务关闭后仍能聊天');
    assert.equal(flatten(offline.session.deriveMessages().at(-1)), '合成测试回复');
    await until(async () => (await readdir(outboxDir)).filter(name => name.endsWith('.json')).length >= 2, 'durable offline outbox');
    await startBackend(modelPort);
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['offline']).length === 2, 'outbox replay after backend restart');
    t.diagnostic('PASS: actual Python backend process stopped/restarted; chat succeeds offline; durable events replay');

    await ctx.fiber.dispose();
    ctx = undefined;
    // Create a persisted session while capture is disabled, then resume it with
    // capture enabled. This exercises the public paged persistence backfill.
    mounted = await mount(serverUrl, false);
    ctx = mounted.context;
    const unobserved = await ctx.agentLoop.create(sessions.SessionId('unobserved'), { provider: 'synthetic', model: 'synthetic' });
    await send(unobserved, '在插件未采集时发生的合成聊天');
    await ctx.fiber.dispose();
    ctx = undefined;
    assert.equal(rows('SELECT * FROM raw_messages WHERE session_id=?', ['unobserved']).length, 0);
    mounted = await mount(serverUrl);
    ctx = mounted.context;
    adapter = mounted.adapter;
    const resumedHandle = await ctx.agentLoop.resume(ctx, { resumeSessionId: sessions.SessionId('unobserved'), agentOptions: { provider: 'synthetic', model: 'synthetic' } });
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['unobserved']).length === 2, 'historical paged backfill');
    await resumedHandle.dispose();
    t.diagnostic('PASS: real JSONL persistence resume backfills previously unobserved human/assistant events');

    const automatic = await ctx.agentLoop.create(sessions.SessionId('automatic'), { provider: 'synthetic', model: 'synthetic' });
    for (let turn = 0; turn < 3; turn++) await send(automatic, `自动整理样本：我曾在山顶看流星雨，第${turn + 1}轮。`);
    await until(() => rows('SELECT * FROM summaries WHERE text=?', ['用户曾在山顶看流星雨']).length >= 1, 'automatic queued consolidation');
    assert(modelInputs.some(input => input.raw_messages.some(message => message.text.includes('自动整理样本'))));
    assert(modelInputs.every(input => input.raw_messages.every(message => !message.text.includes('<goldfish-memory>'))));
    t.diagnostic('PASS: actual assistant event threshold automatically invokes local synthetic Ollama HTTP model and writes evidence-linked summary');
    t.diagnostic(`Runtime: @deepseek-ai/dsh ${version}; synthetic models only; no real Ollama quality or paid cloud API validation`);
  } finally {
    holdRecall?.resolve();
    if (ctx) await ctx.fiber.dispose();
    await stopBackend();
    if (bridge) await closeServer(bridge);
    if (model) await closeServer(model);
    await rm(temp, { recursive: true, force: true });
  }
});
