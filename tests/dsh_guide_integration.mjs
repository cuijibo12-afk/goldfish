/** Actual DSH SDK, JSONL and native compaction; synthetic model fixture only. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { hostModule as load } from '../dsh-plugin/host-sdk.js';
import { KEY, TOOL } from '../dsh-plugin/guide.js';
import * as Goldfish from '../dsh-plugin/index.js';
const [cordis, llm, sessions, projections, prompt, tools, agents, loop, jsonl, meter, compact] = await Promise.all([
  'cordis', 'dsh-llm', 'dsh-session', 'dsh-session-projection', 'dsh-system-prompt', 'dsh-tools', 'dsh-agent',
  'dsh-agent-loop', 'dsh-session-persistence-jsonl', 'dsh-token-meter', 'dsh-compaction-basic',
].map(name => load('@deepseek-ai/' + name)));
const text = message => message.content.filter(b => b.type === 'text').map(b => b.text).join('');
const args = (mainline, expected) => ({ expected_revision: expected, mainline, background: '必要背景',
  progress: [{ text: '用户当前讨论' + mainline, kind: 'user_statement', source_ids: ['current_user'] }],
  open_questions: [], hypotheses: ['可能需要进一步讨论，未确认'], direction: mainline,
  intentions: { continue: [mainline], revise: [], drop: mainline === '天气' ? ['出行选择'] : [] } });

test('C native lifecycle: bounded snapshot, corrections, no-op, cancellation, compaction and JSONL resume', { timeout: 30000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'goldfish-guide-dsh-'));
  let ctx, agent, captured = [], outputs = [], cancelDraft = false, network = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...values) => { network++; return originalFetch(...values); };
  class Adapter extends llm.LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 100000 } }; }
    async *stream(options) {
      captured.push(options);
      const human = options.messages.findLast(m => m.source?.kind === 'user');
      const query = text(human);
      const after = options.messages.slice(options.messages.lastIndexOf(human));
      const results = after.filter(m => m.role === 'tool');
      if (results.length) outputs.push(JSON.parse(text(results.at(-1))));
      if (query !== '7加5' && !results.length) {
        const runtime = options.messages.flatMap(m => m.source?.sections || []).filter(s => s.name === 'goldfish:guide').at(-1);
        assert(runtime, 'native dynamic guide context must reach main model');
        const state = JSON.parse(runtime.text);
        const bad = query === '伪造来源';
        const payload = args(query === '转到天气' ? '天气' : '出行选择', state.expected_revision);
        if (bad) payload.progress[0].source_ids = ['nonexistent-human'];
        yield { type: 'block-start', index: 0, blockType: 'tool-call' };
        yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: llm.ToolCallId('guide-' + captured.length), name: TOOL, arguments: JSON.stringify(payload) } };
        yield { type: 'finish', reason: { kind: 'tool-calls' } }; return;
      }
      yield { type: 'block-start', index: 0, blockType: 'text' };
      yield { type: 'block-end', index: 0, block: { type: 'text', text: query === '7加5' ? '12' : '完成' } };
      yield { type: 'finish', reason: { kind: 'stop' } };
    }
  }
  async function mount() {
    ctx = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) await ctx.plugin(plugin);
    await ctx.plugin(jsonl, { root, compression: 'none' });
    await ctx.plugin(loop.default, { agents: [] }); await ctx.plugin(meter.default);
    ctx.llm.registerAdapter(['synthetic'], new Adapter());
    await ctx.plugin(Goldfish, { enableGuide: true, autoCapture: false });
    ctx.on('session/event', (session, event) => {
      if (cancelDraft && event.type === 'tool/result' && text(event.data.message).includes('goldfish_session_guide/v1')) {
        cancelDraft = false; queueMicrotask(() => agent.cancel({ kind: 'user' }));
      }
    });
  }
  const send = async value => { agent.followup(llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: value }] })); await agent.whenIdle(); };
  const guide = () => ctx.sessionProjections.stateOf(agent.session, KEY);
  try {
    await mount();
    agent = await ctx.agentLoop.create(sessions.SessionId('guide-test'), { provider: 'synthetic', model: 'fixture' });
    await send('7加5'); assert.equal(captured.length, 1); assert.equal(guide().current, null);
    await send('出行选择'); assert.equal(outputs.at(-1).ok, true, JSON.stringify(outputs.at(-1)));
    assert.equal(guide().current.revision, 1);
    const stable = guide().current;
    await send('伪造来源'); assert.equal(outputs.at(-1).error, 'source_not_observed');
    assert.deepEqual(guide().current, stable);
    cancelDraft = true; await send('取消本轮');
    assert.deepEqual(guide().current, stable, 'cancelled draft must never become the cross-turn guide');
    assert.equal(guide().active, null);
    await send('转到天气'); assert.equal(guide().current.content.mainline, '天气');
    assert.deepEqual(guide().current.content.intentions.drop, ['出行选择']);
    const saved = structuredClone(guide().current);
    class FixtureCompaction extends compact.BasicCompactionEngine {
      async summarize() { return { summary: [{ type: 'text', text: '合成压缩：已转向天气。' }], provider: 'synthetic', model: 'fixture' }; }
    }
    const engine = new FixtureCompaction(ctx, { auto: false });
    assert(await engine.compactNow(agent, new AbortController().signal));
    assert.deepEqual(guide().current, saved);
    await send('7加5');
    const runtime = captured.at(-1).messages.flatMap(m => m.source?.sections || []).filter(s => s.name === 'goldfish:guide');
    assert.equal(runtime.length, 1, 'native context snapshots replace the prior guide section');
    assert.equal(JSON.parse(runtime[0].text).guide.content.mainline, '天气');
    await ctx.fiber.dispose(); ctx = undefined;
    await mount();
    const restored = await ctx.agentLoop.resume(ctx, { resumeSessionId: sessions.SessionId('guide-test'), agentOptions: { provider: 'synthetic', model: 'fixture' } });
    agent = restored.agent;
    assert.deepEqual(guide().current, saved, 'pure projection must restore from ordinary JSONL events');
    await send('7加5');
    assert.equal(guide().active, null); assert.equal(network, 0, 'guide never calls a backend/model service');
    await restored.dispose();
    t.diagnostic('Actual DSH projections + dynamic context + JSONL + native compaction passed; cancelled drafts, forged source and no-op isolation passed. Model is a scripted fixture, no external inference.');
  } finally {
    await ctx?.fiber.dispose(); globalThis.fetch = originalFetch;
    await rm(root, { recursive: true, force: true });
  }
});
