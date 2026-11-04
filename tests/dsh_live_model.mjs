/**
 * Opt-in paid-model evaluation: official DSH host, real HTTP model replies,
 * real Python consolidation, and the unmodified BasicCompactionEngine.
 * Run directly with Node (not node --test); see docs/DSH_LIVE_MODEL.md.
 * Every human message is an explicitly synthetic evaluation fixture.
 */
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import * as Goldfish from '../dsh-plugin/index.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const started = new Date().toISOString();
const artifactDir = resolve(process.env.DSH_LIVE_ARTIFACT_DIR || join(root, 'artifacts', 'dsh-live-model', started.replaceAll(/[:.]/g, '-')));
const report = {
  schema: 'goldfish.dsh-live-model.v1', started_at: started, status: 'starting',
  fixture_notice: 'All human inputs and historical seed messages are synthetic fixtures. Assistant replies, consolidation output, and compaction summaries must come from real HTTP model responses.',
  scope: 'Real DSH core host, not the interactive DSH CLI/UI. No memory tools. Live automatic consolidation remains enabled and each turn waits for its completion.',
  budgets: { conversation: 6, compaction: 1, consolidation: 7, semantic_recall: 6, relation_validation: 10, embedding: 30, batch_dedup: 6 },
  calls: [], checks: [], turns: [], recall_calls: [], limitations: [
    'Coverage checks measure documented literal/alias cue survival, not complete fact, relation, scene, date, or negation correctness. Unmatched English paraphrases require human review.',
    'One real compaction is observed without forcing the model to drop or retain any fact. A retained fact is not an omission/reinjection success.',
    'Embedding is exercised only when explicitly configured. No cancellation/retry/outage scenario or personal DSH profile is exercised by this runner.',
  ],
};
const compactionTokens = Number(process.env.DSH_LIVE_COMPACTION_TOKENS || 2048);
if (!Number.isInteger(compactionTokens) || compactionTokens < 64 || compactionTokens > 8192) {
  throw new Error('DSH_LIVE_COMPACTION_TOKENS must be an integer between 64 and 8192');
}
report.compaction_output_limit = compactionTokens;
const briefCompaction = process.env.DSH_LIVE_BRIEF_COMPACTION === '1';
const briefInstruction = '这是隔离评测中的有损压缩压力场景。请用不超过120个中文字符，只保留讨论主题、用户当前要求和任务进度；具体经历细节交由外部长期记忆补充，不保留人名、精确日期、物品编号或票据位置。请正常结束，不要截断。';
report.compaction_prompt_addendum = briefCompaction ? briefInstruction : null;
const first = (...values) => values.find(value => typeof value === 'string' && value.trim())?.trim() || '';
const mainConfig = {
  url: first(process.env.DSH_LIVE_API_URL, process.env.DEEPSEEK_BASE_URL, process.env.OPENAI_BASE_URL, process.env.GOLDFISH_LLM_URL),
  key: first(process.env.DSH_LIVE_API_KEY, process.env.DEEPSEEK_API_KEY, process.env.OPENAI_API_KEY, process.env.GOLDFISH_LLM_API_KEY),
  model: first(process.env.DSH_LIVE_MODEL, process.env.DEEPSEEK_MODEL, process.env.OPENAI_MODEL, process.env.GOLDFISH_LLM_MODEL),
  thinking: first(process.env.DSH_LIVE_THINKING, process.env.GOLDFISH_LLM_THINKING, 'disabled'),
};
const extractionConfig = {
  url: first(process.env.GOLDFISH_LLM_URL, mainConfig.url),
  key: first(process.env.GOLDFISH_LLM_API_KEY, mainConfig.key),
  model: first(process.env.GOLDFISH_LLM_MODEL, mainConfig.model),
};
const embeddingConfig = { url: process.env.GOLDFISH_EMBED_URL || '', key: process.env.GOLDFISH_EMBED_API_KEY || '', model: process.env.GOLDFISH_EMBED_MODEL || '' };
const secrets = [mainConfig.key, extractionConfig.key, embeddingConfig.key, process.env.GOLDFISH_LOCAL_TOKEN].filter(Boolean);
const safeText = text => secrets.reduce((value, secret) => value.replaceAll(secret, '[REDACTED]'), String(text));
const safeError = error => ({ name: error?.name || 'Error', message: safeText(error?.message || 'Unknown error').slice(0, 500) });
const flatten = message => (message?.content || []).filter(block => block.type === 'text').map(block => block.text).join('');
const memoryMessages = messages => messages.filter(message => message.source?.kind === 'plugin:goldfish-memory');
const check = (name, passed, details) => report.checks.push({ name, passed: Boolean(passed), ...details === undefined ? {} : { details } });
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(test, description, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await test()) return; await pause(30); }
  throw new Error('Timed out: ' + description);
}
async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}
async function closeServer(server) {
  if (!server?.listening) return;
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
function endpoint(base) {
  const url = new URL(base);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Model URL must be HTTP(S) without credentials, query, or fragment');
  if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error('Live runner requires HTTPS for remote model endpoints');
  const value = base.replace(/\/+$/, '');
  return value.endsWith('/chat/completions') ? value : value.endsWith('/v1') ? value + '/chat/completions' : value + '/v1/chat/completions';
}
function literalCoverage(text, facts) {
  const normalized = text.toLowerCase().replaceAll(/\s+/g, ' ');
  return facts.map(fact => ({ id: fact.id, description: fact.description, scope: 'literal_cue_only',
    matched: fact.aliases.some(alias => normalized.includes(alias.toLowerCase())),
    matched_aliases: fact.aliases.filter(alias => normalized.includes(alias.toLowerCase())) }));
}
function usageForDsh(usage) {
  if (!usage) return undefined;
  const read = usage.prompt_cache_hit_tokens ?? usage.prompt_tokens_details?.cached_tokens ?? 0;
  const write = usage.prompt_cache_write_tokens ?? 0;
  return { inputTokens: Math.max(0, (usage.prompt_tokens ?? 0) - read - write),
    outputTokens: usage.completion_tokens ?? 0,
    ...usage.total_tokens === undefined ? {} : { totalTokens: usage.total_tokens },
    ...read ? { cacheReadTokens: read } : {}, ...write ? { cacheWriteTokens: write } : {},
    ...usage.completion_tokens_details?.reasoning_tokens === undefined ? {} : { reasoningTokens: usage.completion_tokens_details.reasoning_tokens } };
}
async function callModel(config, payload, purpose, signal) {
  const record = { index: report.calls.length + 1, purpose, requested_model: payload.model,
    request: structuredClone(payload), started_at: new Date().toISOString() };
  report.calls.push(record);
  if (report.calls.filter(call => call.purpose === purpose).length > report.budgets[purpose]) {
    record.error = { name: 'BudgetExceeded', message: purpose + ' call budget exhausted before HTTP' };
    throw new Error(record.error.message);
  }
  const begin = performance.now();
  let stage = 'transport';
  try {
    record.http_attempted = true;
    const target = purpose === 'embedding' ? config.url.replace(/\/$/, '').replace(/\/v1$/, '') + '/v1/embeddings' : endpoint(config.url);
    const response = await fetch(target, { method: 'POST', redirect: 'error',
      headers: { 'Content-Type': 'application/json', ...config.key ? { Authorization: 'Bearer ' + config.key } : {} },
      body: JSON.stringify(payload), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(90000)]) : AbortSignal.timeout(90000) });
    record.http_status = response.status;
    stage = 'http_status';
    // Error bodies and headers may contain provider diagnostics or credentials.
    if (!response.ok) { await response.body?.cancel(); throw new Error('Model endpoint returned HTTP ' + response.status); }
    stage = 'response_json';
    const data = await response.json();
    record.response = purpose === 'embedding' ? { model: data.model, usage: data.usage, finish_reason: 'embedding_complete', count: data.data?.length, dim: data.data?.[0]?.embedding?.length } : { id: data.id, model: data.model, usage: data.usage,
      finish_reason: data.choices?.[0]?.finish_reason, text: data.choices?.[0]?.message?.content };
    record.dsh_usage = usageForDsh(data.usage);
    stage = 'response_validation';
    if (purpose === 'embedding' ? !record.response.count : typeof record.response.text !== 'string' || !record.response.text.trim()) throw new Error('Model response missing content');
    return { data, record };
  } catch (error) {
    // Never persist a nested network error: its message, request, or headers
    // can contain the destination or authentication. Retain only a code atom.
    const causeCode = typeof error?.cause?.code === 'string' && /^[A-Z0-9_]{1,80}$/.test(error.cause.code) ? error.cause.code : undefined;
    record.error = { name: error?.name || 'Error', stage,
      message: stage === 'transport' ? 'Model transport failed' : stage === 'response_json' ? 'Model endpoint returned invalid JSON' : safeError(error).message,
      ...causeCode === undefined ? {} : { cause_code: causeCode } };
    throw new Error(record.error.message, causeCode === undefined ? undefined : { cause: { code: causeCode } });
  }
  finally { record.latency_ms = Math.round((performance.now() - begin) * 10) / 10; }
}
async function saveReport() {
  report.finished_at = new Date().toISOString();
  report.totals = Object.fromEntries(Object.keys(report.budgets).map(purpose => {
    const calls = report.calls.filter(call => call.purpose === purpose);
    return [purpose, { attempted: calls.length, http_attempted: calls.filter(call => call.http_attempted).length,
      http_succeeded: calls.filter(call => call.http_status >= 200 && call.http_status < 300).length,
      successful: calls.filter(call => call.response && !call.error && ['stop','embedding_complete'].includes(call.response.finish_reason)).length,
      truncated: calls.filter(call => call.response?.finish_reason === 'length').length,
      latency_ms: Math.round(calls.reduce((sum, call) => sum + (call.latency_ms || 0), 0)),
      prompt_tokens: calls.reduce((sum, call) => sum + (call.response?.usage?.prompt_tokens || 0), 0),
      completion_tokens: calls.reduce((sum, call) => sum + (call.response?.usage?.completion_tokens || 0), 0),
      total_tokens: calls.reduce((sum, call) => sum + (call.response?.usage?.total_tokens || 0), 0),
      missing_usage_calls: calls.filter(call => call.response && !call.response.usage).length }];
  }));
  await mkdir(artifactDir, { recursive: true });
  await writeFile(join(artifactDir, 'report.json'), safeText(JSON.stringify(report, null, 2)) + '\n', { mode: 0o600 });
  console.log(JSON.stringify({ status: report.status, report: join(artifactDir, 'report.json'),
    failed_checks: report.checks.filter(item => !item.passed).map(item => item.name), totals: report.totals }));
}

const missing = [!process.env.DSH_TEST_RUNTIME && 'DSH_TEST_RUNTIME', !mainConfig.url && 'model URL',
  !mainConfig.model && 'model name', !mainConfig.key && 'API key'].filter(Boolean);
if (missing.length) {
  report.status = 'skipped'; report.skip_reason = 'Missing explicit real-model configuration: ' + missing.join(', ') + '. No mock or model call was made.';
  await saveReport();
} else {
  let temp, ctx, backend, forwarder, bridge;
  let backendOutput = '';
  const python = process.env.PYTHON || 'python';
  try {
    endpoint(mainConfig.url); endpoint(extractionConfig.url);
    if (embeddingConfig.url) endpoint(embeddingConfig.url);
    if (process.env.GOLDFISH_LLM_PROVIDER && process.env.GOLDFISH_LLM_PROVIDER !== 'openai') throw new Error('This runner requires the openai-compatible consolidation provider');
    const requireRuntime = createRequire(join(resolve(process.env.DSH_TEST_RUNTIME), 'package.json'));
    const version = requireRuntime('@deepseek-ai/dsh/package.json').version;
    if (version !== '0.2.1-rc.5') throw new Error('This live runner is pinned to @deepseek-ai/dsh@0.2.1-rc.5');
    report.runtime = { package: '@deepseek-ai/dsh', version, node: process.version,
      env_proxy_enabled: process.execArgv.includes('--use-env-proxy') || process.env.NODE_USE_ENV_PROXY === '1',
      adapter_context_window: { tokens: 100000, source: 'conservative evaluation declaration; not discovered from the remote model' },
      main_max_tokens: 256, compaction_max_tokens: 2048, automatic_pressure_compaction: false };
    report.models = { conversation: mainConfig.model, compaction: mainConfig.model, consolidation: extractionConfig.model };
    const load = name => import(pathToFileURL(requireRuntime.resolve(name)));
    const [cordis, llm, sessions, projections, prompt, tools, agents, loop, jsonl, meter, compact] = await Promise.all([
      '@deepseek-ai/cordis', '@deepseek-ai/dsh-llm', '@deepseek-ai/dsh-session',
      '@deepseek-ai/dsh-session-projection', '@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools',
      '@deepseek-ai/dsh-agent', '@deepseek-ai/dsh-agent-loop', '@deepseek-ai/dsh-session-persistence-jsonl',
      '@deepseek-ai/dsh-token-meter', '@deepseek-ai/dsh-compaction-basic',
    ].map(load));
    temp = await mkdtemp(join(tmpdir(), 'goldfish-live-model-'));
    const db = join(temp, 'memory.sqlite3');
    const runPython = (code, args = []) => execFileSync(python, ['-c', code, ...args], { cwd: root, encoding: 'utf8' });
    const rows = (sql, params = []) => JSON.parse(runPython(
      'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); c.row_factory=sqlite3.Row; print(json.dumps([dict(x) for x in c.execute(sys.argv[2],json.loads(sys.argv[3]))],ensure_ascii=False))',
      [db, sql, JSON.stringify(params)]));
    const reservation = createServer();
    const backendPort = await listen(reservation); await closeServer(reservation);
    const backendUrl = `http://127.0.0.1:${backendPort}`;
    async function stopBackend() {
      if (!backend || backend.exitCode !== null) return;
      const ended = once(backend, 'exit'); backend.kill('SIGTERM'); await ended;
    }
    async function startBackend(extractionUrl = '') {
      const env = { ...process.env, PYTHONPATH: root };
      for (const name of Object.keys(env)) if (name.startsWith('GOLDFISH_')) delete env[name];
      for (const name of ['DSH_LIVE_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY']) delete env[name];
      if (extractionUrl) Object.assign(env, { GOLDFISH_LLM_PROVIDER: 'openai', GOLDFISH_LLM_URL: extractionUrl,
        GOLDFISH_LLM_MODEL: extractionConfig.model,
        GOLDFISH_SEMANTIC_RECALL: process.env.GOLDFISH_SEMANTIC_RECALL || '0',
        GOLDFISH_RETRIEVAL_MODE: process.env.GOLDFISH_RETRIEVAL_MODE || 'legacy',
        GOLDFISH_RELATION_REVIEW: process.env.GOLDFISH_RELATION_REVIEW || '1',
        ...process.env.GOLDFISH_LLM_THINKING ? { GOLDFISH_LLM_THINKING: process.env.GOLDFISH_LLM_THINKING } : {},
        ...process.env.GOLDFISH_LLM_MAX_TOKENS ? { GOLDFISH_LLM_MAX_TOKENS: process.env.GOLDFISH_LLM_MAX_TOKENS } : {} });
      if (embeddingConfig.url && embeddingConfig.model && extractionUrl) Object.assign(env, {
        GOLDFISH_EMBED_PROVIDER: 'openai', GOLDFISH_EMBED_URL: extractionUrl, GOLDFISH_EMBED_MODEL: embeddingConfig.model,
        GOLDFISH_EMBED_REVISION: process.env.GOLDFISH_EMBED_REVISION || '',
        GOLDFISH_EMBED_QUERY_PREFIX: process.env.GOLDFISH_EMBED_QUERY_PREFIX || '' });
      backendOutput = '';
      backend = spawn(python, ['-m', 'goldfish.server', '--port', String(backendPort), '--db', db], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
      backend.stdout.on('data', data => { backendOutput = (backendOutput + data).slice(-4000); });
      backend.stderr.on('data', data => { backendOutput = (backendOutput + data).slice(-4000); });
      await until(async () => {
        if (backend.exitCode !== null) throw new Error('Python backend startup failed (output omitted; may include local paths)');
        try { return (await fetch(backendUrl + '/health')).ok; } catch { return false; }
      }, 'Python backend startup');
    }
    async function post(path, payload) {
      const response = await fetch(backendUrl + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal: AbortSignal.timeout(90000) });
      const result = await response.json();
      if (!response.ok) throw new Error('Goldfish ' + path + ' returned HTTP ' + response.status + ' (' + result.error + ')');
      return result;
    }
    // Only a transport/metrics shim: extraction payload and real response are
    // forwarded unchanged. No request/response header is retained in the report.
    forwarder = createServer(async (request, response) => {
      try {
        if (!['/v1/chat/completions','/v1/embeddings'].includes(request.url)) { response.writeHead(404); response.end('{}'); return; }
        const chunks = []; for await (const chunk of request) chunks.push(chunk);
        const payload = JSON.parse(Buffer.concat(chunks));
        const system = payload.messages?.[0]?.content || '';
        const purpose = request.url === '/v1/embeddings' ? 'embedding' : system.startsWith('只审核同批候选') ? 'batch_dedup' : system.startsWith('你是长期记忆检索') ? 'semantic_recall'
          : system.startsWith('审核概括之间的关系') ? 'relation_validation' : 'consolidation';
        const { data } = await callModel(purpose === 'embedding' ? embeddingConfig : extractionConfig, payload, purpose);
        response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(data));
      } catch { response.writeHead(502, { 'Content-Type': 'application/json' }); response.end('{"error":"live_model_request_failed"}'); }
    });
    await startBackend(`http://127.0.0.1:${await listen(forwarder)}`);
    report.seed = [
      { id: 'fixture-old-pier', role: 'user', text: '【合成评测历史，非真人经历】2025年10月18日，我和朋友林岚去旧码头散步。我点了桂花拿铁，杯套上写着FJ-7315。那天我把一张蓝色地铁票夹进纸质笔记本，并在本子里记下当时想转岗但还没做决定。这只是一次具体经历，不能把喝咖啡当作转岗的原因。' },
      { id: 'fixture-mountain', role: 'user', text: '【合成评测历史，非真人经历】2026年2月9日，我又和林岚去北山看流星雨，也带了那本纸质笔记本。这次喝的是热可可，不是桂花拿铁。票仍夹在本子里，灯光很暗，我没有在现场补写工作决定。北山和旧码头是不同日期的两次经历，不能合并成同一次，也不能据此捏造因果。' },
      { id: 'fixture-feedback', role: 'user', text: '【合成评测历史，非真人经历】你刚才的回答太机械。这是对刚才那次回答的反馈。另外我希望你今后不要总附和我，有依据时请指出不同看法。这是我的交互期望，不是在描述你天生爱反驳。' },
    ];
    for (const message of report.seed) await post('/ingest', { session_id: 'synthetic-historical-fixture', message_id: message.id, role: message.role, text: message.text, source: 'human', consolidate_after: false });
    report.consolidation = await post('/consolidate', { session_id: 'synthetic-historical-fixture' });
    report.consolidated_corpus = { summaries: rows('SELECT * FROM summaries'), evidence: rows('SELECT * FROM evidence'),
      themes: rows('SELECT * FROM themes'), profiles: rows('SELECT * FROM profile'), links: rows('SELECT * FROM links') };
    check('real_consolidation_created_evidence_linked_summaries', report.consolidated_corpus.summaries.length > 0 && report.consolidated_corpus.summaries.every(summary => report.consolidated_corpus.evidence.some(evidence => evidence.summary_id === summary.id)));
    bridge = createServer(async (request, response) => {
      try {
        const chunks = []; for await (const chunk of request) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        const record = request.url === '/recall' ? { request: JSON.parse(body), started_at: new Date().toISOString() } : undefined;
        const begin = performance.now();
        if (record) report.recall_calls.push(record);
        const upstream = await fetch(backendUrl + request.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
        const text = await upstream.text();
        if (record) Object.assign(record, { latency_ms: Math.round((performance.now() - begin) * 10) / 10, http_status: upstream.status, response: JSON.parse(text) });
        response.writeHead(upstream.status, { 'Content-Type': 'application/json' }); response.end(text);
      } catch { if (!response.destroyed) { response.writeHead(503); response.end('{}'); } }
    });
    const serverUrl = `http://127.0.0.1:${await listen(bridge)}`;
    class LiveAdapter extends llm.LlmAdapter {
      requests = [];
      async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 100000 }, defaultMaxTokens: 256 }; }
      async *stream(options) {
        const purpose = options.purpose === 'compaction' ? 'compaction' : 'conversation';
        const messages = options.messages.map(message => {
          if (message.content.some(block => block.type !== 'text')) throw new Error('Live fixture supports text messages only');
          return { role: message.role, content: flatten(message) };
        });
        const observation = { purpose, dsh_messages: structuredClone(options.messages), memory_message_count: memoryMessages(options.messages).length };
        this.requests.push(observation);
        const payload = { model: options.model, messages, stream: false, temperature: 0.1, max_tokens: options.maxTokens ?? 256,
          thinking: { type: mainConfig.thinking } };
        if (purpose === 'compaction' && briefCompaction) payload.messages.push({ role: 'user', content: briefInstruction });
        const { data, record } = await callModel(mainConfig, payload, purpose, options.signal);
        observation.call_index = record.index;
        const choice = data.choices[0];
        if (choice.message.tool_calls?.length) throw new Error('Unexpected tool call in text-only live fixture');
        const text = choice.message.content;
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text };
        yield { type: 'block-end', index: 0, block: { type: 'text', text } };
        const usage = usageForDsh(data.usage);
        if (usage) yield { type: 'usage', usage };
        const reason = choice.finish_reason === 'stop' ? { kind: 'stop' } : choice.finish_reason === 'length' ? { kind: 'max-tokens' }
          : { kind: 'error', failure: { message: 'Unexpected provider finish reason', code: 'UNKNOWN' } };
        yield { type: 'finish', reason };
      }
    }
    ctx = new cordis.Context();
    for (const plugin of [llm.default, sessions.default, projections.default, prompt.default, tools.default, agents.default]) await ctx.plugin(plugin);
    await ctx.plugin(jsonl, { root: join(temp, 'sessions'), compression: 'none' });
    await ctx.plugin(loop.default, { agents: [] }); await ctx.plugin(meter.default);
    const adapter = new LiveAdapter(); ctx.llm.registerAdapter(['live-openai'], adapter);
    await ctx.plugin(Goldfish, { serverUrl, token: '', outboxDir: join(temp, 'outbox'), timeoutMs: 12000, retryMs: 100, autoCapture: true, limit: 3, maxChars: 2400 });
    const agent = await ctx.agentLoop.create(sessions.SessionId('synthetic-live-six-turns'), { provider: 'live-openai', model: mainConfig.model, maxTokens: 256 });
    const facts = [
      { id: 'old_pier', description: '旧码头地点', aliases: ['旧码头', 'old pier', 'old wharf'] },
      { id: 'drink', description: '桂花拿铁词语线索；未验证场景或否定关系', aliases: ['桂花拿铁', 'osmanthus latte', 'osmanthus-flavored latte'] },
      { id: 'cup_code', description: '杯套号码FJ-7315', aliases: ['FJ-7315'] },
      { id: 'ticket', description: '蓝色地铁票词语线索；未验证夹入笔记本的关系', aliases: ['蓝色地铁票', 'blue subway ticket', 'blue metro ticket'] },
      { id: 'friend', description: '同行人林岚', aliases: ['林岚', 'lin lan', 'linlan'] },
      { id: 'pier_date', description: '旧码头日期2025-10-18', aliases: ['2025-10-18', '2025年10月18日', 'october 18, 2025', '18 october 2025', 'oct 18, 2025'] },
    ];
    report.fact_criteria = facts;
    report.consolidated_fact_coverage = literalCoverage(report.consolidated_corpus.summaries.map(summary => summary.text).join('\n'), facts);
    const injectionEvents = () => agent.session.snapshotEvents().filter(event => event.type === 'user/message' && event.data.source?.kind === 'plugin:goldfish-memory');
    const filler = '这是合成评测的上下文填充段：我们只是放慢讨论速度，逐项区分已知信息和还不确定的信息；这段话没有新增个人经历，也不要求你创造事实。'.repeat(9);
    const questions = [
      '还记得我和林岚去旧码头那次经历吗？请根据已有记忆简短回应。',
      '继续聊旧码头那次经历，先说哪些细节有来源，别猜原因。',
      '还是旧码头那件事，帮我把明确发生的事情与未决定的事情分开。',
      '旧码头的经历里有哪些小细节值得保留？简短回答即可。',
      '我们继续讨论旧码头，不加新事实，也别把北山那次混进来。',
    ];
    if (briefCompaction) questions[4] = '我们继续讨论旧码头，不加新事实也不混入北山。这一轮只简短确认话题，请不要复述编号、日期或票据位置。';
    async function send(question, phase) {
      const before = injectionEvents().length;
      const text = `【合成评测用户输入】${question}\n${phase === 'five-turn-dedup' ? filler : ''}`;
      const callCount = adapter.requests.length;
      const assistantsBefore = agent.session.snapshotEvents().filter(event => event.type === 'assistant/message').length;
      agent.followup(llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] }));
      await agent.whenIdle();
      const requests = adapter.requests.slice(callCount);
      const responses = agent.session.snapshotEvents().filter(event => event.type === 'assistant/message').slice(assistantsBefore);
      const response = responses.at(-1);
      const observation = { turn: report.turns.length + 1, phase, synthetic_user_text: text,
        added_memory_messages: injectionEvents().length - before,
        new_injected_messages: structuredClone(injectionEvents().slice(before).map(event => event.data)),
        requests, actual_assistant_text: response ? flatten(response.data.message) : null };
      report.turns.push(observation);
      check('turn_' + observation.turn + '_one_real_conversation_request', requests.length === 1 && requests[0].purpose === 'conversation');
      check('turn_' + observation.turn + '_actual_assistant_output', requests.length > 0 && Boolean(observation.actual_assistant_text));
      // Poll the real durable extraction cursor, not just an HTTP request count.
      // The next user turn cannot race a still-running successful extraction.
      const consolidationStarted = performance.now();
      try {
        await until(async () => {
          const raw = rows('SELECT COUNT(*) AS count, MAX(seq) AS last_seq FROM raw_messages WHERE session_id=?', ['synthetic-live-six-turns'])[0];
          const job = rows('SELECT * FROM consolidation_jobs WHERE session_id=?', ['synthetic-live-six-turns'])[0];
          return raw.count === observation.turn * 2 && job && job.last_seq >= raw.last_seq;
        }, 'automatic consolidation of turn ' + observation.turn, 75000);
        observation.auto_consolidation_completed = true;
      } catch (error) {
        observation.auto_consolidation_completed = false;
        observation.auto_consolidation_error = safeError(error);
      }
      observation.auto_consolidation_wait_ms = Math.round((performance.now() - consolidationStarted) * 10) / 10;
      observation.consolidation_job = rows('SELECT * FROM consolidation_jobs WHERE session_id=?', ['synthetic-live-six-turns'])[0];
      observation.corpus_after_turn = { summaries: rows('SELECT * FROM summaries'), evidence: rows('SELECT * FROM evidence'),
        themes: rows('SELECT * FROM themes'), profiles: rows('SELECT * FROM profile'), links: rows('SELECT * FROM links') };
      check('turn_' + observation.turn + '_real_automatic_consolidation_completed', observation.auto_consolidation_completed,
        { job: observation.consolidation_job, summary_count: observation.corpus_after_turn.summaries.length });
      console.log(JSON.stringify({ progress: phase, turn: observation.turn, new_injections: observation.added_memory_messages,
        auto_consolidation_completed: observation.auto_consolidation_completed, summaries: observation.corpus_after_turn.summaries.length }));
    }
    for (const question of questions) await send(question, 'five-turn-dedup');
    check('five_turns_one_durable_injection', injectionEvents().length === 1, { injection_count: injectionEvents().length });
    check('five_model_inputs_one_existing_memory_message_each', report.turns.every(turn => turn.requests.length === 1 && turn.requests[0].memory_message_count === 1),
      { counts: report.turns.map(turn => turn.requests.map(request => request.memory_message_count)) });
    const beforeMessages = structuredClone(agent.session.deriveMessages());
    const beforeText = beforeMessages.map(flatten).join('\n');
    const corpusBeforeCompaction = report.turns.at(-1).corpus_after_turn.summaries;
    report.compaction = { engine: 'unmodified BasicCompactionEngine.compactNow', before_messages: beforeMessages,
      before_fact_coverage: literalCoverage(beforeText, facts), before_memory_message_count: memoryMessages(beforeMessages).length };
    const engine = new compact.BasicCompactionEngine(ctx, { auto: false, maxTokens: compactionTokens, compactionRetries: 0 });
    try {
      const begin = performance.now();
      const result = await engine.compactNow(agent, AbortSignal.timeout(90000));
      report.compaction.elapsed_ms = Math.round((performance.now() - begin) * 10) / 10;
      report.compaction.result = result;
      report.compaction.performed = Boolean(result);
    } catch (error) { report.compaction.error = safeError(error); report.compaction.performed = false; }
    const afterMessages = structuredClone(agent.session.deriveMessages());
    const afterText = afterMessages.map(flatten).join('\n');
    Object.assign(report.compaction, { after_messages: afterMessages, after_fact_coverage: literalCoverage(afterText, facts),
      after_memory_message_count: memoryMessages(afterMessages).length,
      summary_literal_retention: corpusBeforeCompaction.map(summary => ({ id: summary.id, text: summary.text,
        present_before: beforeText.includes(summary.text), present_after: afterText.includes(summary.text) })),
      events: agent.session.snapshotEvents().filter(event => event.type.startsWith('compaction/')) });
    const missingFacts = report.compaction.before_fact_coverage.filter(before => before.matched && !report.compaction.after_fact_coverage.find(after => after.id === before.id).matched).map(fact => fact.id);
    report.compaction.literal_omissions = missingFacts;
    check('official_compaction_committed_real_model_summary', report.compaction.performed && report.calls.some(call => call.purpose === 'compaction' && call.response));
    await send('旧码头那杯桂花拿铁的杯套编号是什么？蓝色地铁票放在哪里？请根据现在可见的证据回答。', 'post-compaction');
    const postTurn = report.turns.at(-1);
    const postInput = postTurn.requests.at(-1)?.dsh_messages || [];
    const postCoverage = literalCoverage(postInput.map(flatten).join('\n'), facts);
    report.compaction.post_turn_fact_coverage = postCoverage;
    report.compaction.omission_recovery = missingFacts.map(id => ({ id, visible_after_recall: postCoverage.find(fact => fact.id === id).matched }));
    report.compaction.reinjection_observation = { new_memory_messages: postTurn.added_memory_messages,
      actual_input_memory_message_count: memoryMessages(postInput).length,
      full_summary_exactly_retained: corpusBeforeCompaction.filter(summary => afterText.includes(summary.text)).map(summary => summary.id),
      evaluation: !report.compaction.performed ? 'compaction_failed; reinjection scenario not established'
        : missingFacts.length ? 'literal omissions observed; inspect omission_recovery and recall response for recovery or failure'
        : 'no literal omission observed; this run cannot establish missing-fact reinjection' };
    if (missingFacts.length && report.compaction.performed) check('observed_literal_omission_cues_recovered_in_actual_input', report.compaction.omission_recovery.every(fact => fact.visible_after_recall));
    if (report.compaction.performed) {
      const retained = corpusBeforeCompaction.filter(summary => afterText.includes(summary.text));
      const newlyInjectedText = postTurn.new_injected_messages.map(flatten).join('\n');
      report.compaction.retained_summary_duplicate_injections = retained.filter(summary => newlyInjectedText.includes(summary.text)).map(summary => summary.id);
      if (retained.length) check('verbatim_retained_summaries_not_duplicated_after_compaction', report.compaction.retained_summary_duplicate_injections.length === 0,
        { retained_summary_count: retained.length, duplicated_ids: report.compaction.retained_summary_duplicate_injections });
    }
    await until(() => rows('SELECT * FROM raw_messages WHERE session_id=?', ['synthetic-live-six-turns']).length >= 12, 'capture six synthetic user inputs and six real assistant replies');
    report.captured_messages = rows('SELECT * FROM raw_messages WHERE session_id=? ORDER BY seq', ['synthetic-live-six-turns']);
    check('capture_twelve_raw_messages_without_memory_pollution', report.captured_messages.length === 12 && report.captured_messages.every(message => !message.text.includes('<goldfish-memory>')));
    check('captured_assistant_text_matches_real_model_output', JSON.stringify(report.captured_messages.filter(message => message.role === 'assistant').map(message => message.text)) === JSON.stringify(report.turns.map(turn => turn.actual_assistant_text)));
    report.status = report.checks.every(item => item.passed) ? 'completed' : 'completed_with_findings';
  } catch (error) {
    report.status = 'failed'; report.error = safeError(error); process.exitCode = 1;
  } finally {
    try { if (ctx) await ctx.fiber.dispose(); } catch (error) { report.cleanup_error = safeError(error); }
    if (backend && backend.exitCode === null) { const ended = once(backend, 'exit'); backend.kill('SIGTERM'); await ended; }
    await closeServer(bridge); await closeServer(forwarder);
    if (temp) await rm(temp, { recursive: true, force: true });
    await saveReport();
  }
}
