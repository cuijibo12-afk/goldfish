/** C: the current DSH Agent authors one bounded guide; native events persist it.
 * No model client, background agent, private reasoning reader, DB or sidecar. */
import { hostModule } from './host-sdk.js';
import { resolveMemoryRef, memoryRef } from './memory-refs.js';

export const KEY = 'goldfishGuide';
export const TOOL = 'goldfish_guide_update';
const READS = new Set(['goldfish_search', 'goldfish_explore', 'goldfish_evidence']);
const optional = (ctx, name) => typeof ctx.get === 'function' ? ctx.get(name) : ctx[name];
const textOf = message => (message?.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
const parse = text => { try { return JSON.parse(text); } catch { return null; } };
const plain = value => value && typeof value === 'object' && !Array.isArray(value);
const str = (value, max = 240) => typeof value === 'string' && value.length <= max;
const list = (value, max = 4) => Array.isArray(value) && value.length <= max && value.every(v => str(v, 180));
const FIELDS = ['mainline', 'background', 'progress', 'open_questions', 'hypotheses', 'direction', 'intentions'];

export function validContent(g) {
  return plain(g) && Object.keys(g).length === FIELDS.length && FIELDS.every(k => k in g)
    && str(g.mainline) && str(g.background, 360) && str(g.direction)
    && list(g.open_questions) && list(g.hypotheses)
    && Array.isArray(g.progress) && g.progress.length <= 5 && g.progress.every(p => plain(p)
      && Object.keys(p).length === 3 && str(p.text, 180)
      && ['user_statement', 'memory_statement', 'agent_judgment'].includes(p.kind)
      && Array.isArray(p.source_ids) && p.source_ids.length <= 4 && p.source_ids.every(id => str(id, 200)))
    && plain(g.intentions) && Object.keys(g.intentions).length === 3
    && ['continue', 'revise', 'drop'].every(k => list(g.intentions[k], 3))
    && JSON.stringify(g).length <= 3500;
}

export function initialGuideState(_header, inherited = 0) {
  return { current: null, active: null, inherited, last_completed_turn: null,
    view: { schema: 1, guide: null, last_completed_turn: null } };
}
function revision(state) { return (state.active?.draft || state.current)?.revision || 0; }

export function foldGuide(state, event) {
  const data = event.data;
  if (event.type === 'turn/start') return { ...state, active: {
    turn: data.turn, human: null, calls: {}, memory_ids: [], draft: null } };
  if (event.type === 'turn/end') {
    if (!state.active) return state;
    const completed = data.reason?.kind === 'completed';
    const current = completed && state.active.draft ? state.active.draft : state.current;
    const last = completed ? data.turn : state.last_completed_turn;
    return { ...state, current, active: null, last_completed_turn: last,
      view: { schema: 1, guide: current, last_completed_turn: last } };
  }
  if (!state.active) return state;
  const active = state.active;
  if (event.type === 'user/message' && data.source?.kind === 'user') {
    // A fresh human input supersedes an earlier draft/consent in this turn.
    return { ...state, active: { ...active, human: data.id, draft: null, calls: {}, memory_ids: [] } };
  }
  if (event.type === 'step/start') return { ...state, active: { ...active, calls: {} } };
  if (event.type === 'assistant/message') {
    const calls = { ...active.calls };
    for (const block of data.message.content || []) {
      if (block.type === 'tool-call' && (block.name === TOOL || READS.has(block.name)) && Object.keys(calls).length < 32)
        calls[block.id] = block.name;
    }
    return Object.keys(calls).length ? { ...state, active: { ...active, calls } } : state;
  }
  if (event.type !== 'tool/result') return state;
  const name = active.calls[data.message.toolCallId];
  if (!name) return state;
  const result = parse(textOf(data.message));
  if (!result?.ok) return state;
  if (READS.has(name)) {
    const ids = typeof result.memory_id === 'string' ? [result.memory_id]
      : (result.records || []).map(r => r.id).filter(id => typeof id === 'string');
    return { ...state, active: { ...active, memory_ids: [...new Set([...active.memory_ids, ...ids])].slice(-100) } };
  }
  const g = result.guide;
  if (result.kind !== 'goldfish_session_guide/v1' || !g || !validContent(g.content)
      || g.revision !== revision(state) + 1 || g.based_on_user_message_id !== active.human
      || g.turn !== active.turn || !str(g.session_id, 200)) return state;
  return { ...state, active: { ...active, draft: { ...g, updated_at: new Date(event.time).toISOString() } } };
}

export const POLICY = `金鱼会话协作：你是唯一思考主体。当前轮按正常原生推理理解用户；无需历史时不查金鱼。需要时先窄查 search 阅读主要经历，再依据发现的具体缺口决定是否 explore（以已得memory_ref为种子，提出新的时间变化、意义、相似/相反或跨主题问题），必要时 evidence 核实原话；不机械重复检索，不将线索当确证因果或自动建边。
总指引是DSH会话工作摘要，与长期记忆写入完全独立。每轮审视最新用户和本轮已获结果：出现持续讨论的新主题（包括guide=null时）、背景/进展变化、纠正、转题或否定时，必须在正式回复前调用 goldfish_guide_update 整体替换；保留必要背景，删除被纠正的旧值、失效预测和意图，不在progress堆积旧轮记录。用户说不保存长期记忆不禁止维护这个会话摘要。简单算术/一次性短问答或整体内容无变化时可直接回复、零工具调用。user_statement只引用真人消息ID/current_user；历史检索所得必须为memory_statement并引用原ID或memory_ref；自身判断为agent_judgment，预测只进hypotheses且不当事实。更新若被拒绝，应根据错误纠正来源类型/引用后重试，再正式回复。只提交影响整体方向的简要结果，不提交逐步思考、隐藏推理或临时检索计划。细思考不建立独立持久状态；总指引绝不调用 remember 或进入长期库。`;

function rootAgent(ctx, agent) {
  const registry = optional(ctx, 'agents');
  return !!agent && registry?.get(agent.id) === agent && registry.roots().includes(agent);
}
export function renderGuide(state, proposedHuman) {
  const guide = state.active?.draft || state.current;
  // Only the model-facing view uses short references. The native projection
  // retains canonical source IDs, so aliases still resolve after compaction.
  const modelGuide = guide ? { ...guide, content: { ...guide.content,
    progress: guide.content.progress.map(p => p.kind === 'memory_statement'
      ? { ...p, source_ids: p.source_ids.map(memoryRef) } : p) } } : null;
  return JSON.stringify({ scope: 'DSH_session_working_guide_only',
    current_user: proposedHuman || state.active?.human || null,
    expected_revision: guide?.revision || 0, provisional: !!state.active?.draft,
    guide: modelGuide, hypothesis_status: 'unconfirmed_do_not_promote_to_fact',
    maintenance: 'New continuing topic, correction, topic shift or rejected interpretation requires a guide replacement before final reply. Trivial one-off or unchanged turns may skip. This is NOT long-term memory saving.',
    precedence: 'latest_user_and_original_evidence_override_this_revisable_guide' });
}

export async function registerGuide(ctx) {
  const [{ defineTool }, { z }] = await Promise.all([hostModule('@deepseek-ai/dsh-tools'), hostModule('zod')]);
  const projections = optional(ctx, 'sessionProjections'), prompt = optional(ctx, 'systemPrompt'), tools = optional(ctx, 'tools');
  if (!projections || !prompt || !tools) throw Error('C requires native DSH projections and dynamic context');
  const snapshot = z.custom(s => s === null || (plain(s) && validContent(s.content)
    && Number.isSafeInteger(s.revision) && s.revision > 0 && str(s.session_id, 200)
    && str(s.based_on_user_message_id, 200) && Number.isSafeInteger(s.turn)));
  const viewSchema = z.object({ schema: z.literal(1), guide: snapshot, last_completed_turn: z.number().nullable() });
  const activeSchema = z.object({ turn: z.number(), human: z.string().nullable(),
    calls: z.record(z.string(), z.enum([TOOL, ...READS])).refine(v => Object.keys(v).length <= 32),
    memory_ids: z.array(z.string().max(200)).max(100), draft: snapshot }).strict();
  const stateSchema = z.object({ current: snapshot, active: activeSchema.nullable(), inherited: z.number(),
    last_completed_turn: z.number().nullable(), view: viewSchema });
  projections.register({ key: KEY, stateVersion: 1, stateSchema, init: initialGuideState, apply: foldGuide,
    wire: { viewSchema, view: state => state.view } });
  const proposed = new WeakMap();
  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    const decision = await next();
    if (decision.kind === 'enter' && !signal.aborted && rootAgent(ctx, agent)) {
      const human = decision.messages.findLast(m => m.source?.kind === 'user');
      if (human) proposed.set(agent.session, human.id);
    }
    return decision;
  });
  ctx.on('session/disposed', session => proposed.delete(session));
  prompt.section({ name: 'goldfish:collaboration', order: 2920, interpolate: false,
    text: ({ scope }) => rootAgent(ctx, scope) ? POLICY : '' });
  prompt.context({ name: 'goldfish:guide', order: 2920, required: true, interpolate: false,
    text: ({ scope }) => rootAgent(ctx, scope)
      ? renderGuide(projections.stateOf(scope.session, KEY), proposed.get(scope.session)) : '' });

  const strings = description => ({ type: 'array', items: { type: 'string' }, required: true, description });
  tools.register(defineTool({ name: TOOL,
    description: 'Replace the bounded DSH session guide when overall conversation direction, progress, uncertainties or intentions change. Same main Agent only; not a memory save or a reasoning scratchpad. Read expected_revision/current_user from goldfish:guide context. Submit concise results before final reply; commit occurs only when this human turn completes. Simple unchanged turns need no update. hypotheses remain unconfirmed; user_statement and memory_statement require real source IDs (current_user alias is accepted). Clear outdated items rather than appending history.',
    parameters: {
      expected_revision: { type: 'integer', required: true, description: 'Current guide revision; optimistic concurrency guard.' },
      mainline: { type: 'string', required: true, description: 'Current topic/main thread, at most 240 characters.' },
      background: { type: 'string', required: true, description: 'Only necessary continuing context, at most 360 characters.' },
      progress: { type: 'array', required: true, description: 'At most 5 concise progress/judgment items. No invented verified facts.',
        items: { type: 'object', additionalProperties: false, properties: {
          text: { type: 'string', required: true },
          kind: { type: 'string', enum: ['user_statement', 'memory_statement', 'agent_judgment'], required: true },
          source_ids: strings('Actual user-message IDs/current_user, or retrieved memory IDs. agent_judgment may have none.') } } },
      open_questions: strings('At most 4 unresolved questions; each at most 180 characters.'),
      hypotheses: strings('At most 4 explicitly uncertain possible next conversation directions, each at most 180 characters. When the user asks which directions the discussion might take, preserve your tentative possibilities here. Empty when prediction is unnecessary. Never predictions about fixed personality, hidden motives, or future events asserted as facts.'),
      direction: { type: 'string', required: true, description: 'Current intended conversational direction, at most 240 characters.' },
      intentions: { type: 'object', additionalProperties: false, required: true, properties: {
        continue: strings('At most 3 intentions to continue.'), revise: strings('At most 3 intentions to revise.'),
        drop: strings('At most 3 intentions to abandon, including user-rejected predictions.') } },
    }, output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    isConcurrencySafe: () => false, timeoutMs: 5000,
    async execute(args, exec) {
      try {
        const agent = exec.agent, registry = optional(ctx, 'agents');
        if (!rootAgent(ctx, agent) || registry.currentInitiator() !== agent || agent.status !== 'running' || exec.signal.aborted)
          throw Error('live_root_required');
        const state = projections.stateOf(agent.session, KEY), active = state.active;
        if (!active?.human || args.expected_revision !== revision(state)) throw Error('stale_or_missing_human_turn');
        if (Object.keys(args).some(k => ![...FIELDS, 'expected_revision'].includes(k))) throw Error('unsupported_field');
        const content = Object.fromEntries(FIELDS.map(k => [k, args[k]]));
        if (!validContent(content)) throw Error('bounded_summary_required');
        const humans = new Set(agent.session.deriveMessages().filter(m => m.source?.kind === 'user').map(m => m.id));
        humans.add(active.human);
        for (const p of state.current?.content.progress || []) {
          if (p.kind === 'user_statement') for (const id of p.source_ids) humans.add(id);
        }
        const memories = new Set([...active.memory_ids, ...(state.current?.content.progress || [])
          .filter(p => p.kind === 'memory_statement').flatMap(p => p.source_ids)]);
        content.progress = content.progress.map(p => ({ ...p, source_ids: p.source_ids.map(id => id === 'current_user'
          ? active.human : resolveMemoryRef(id, [...memories])) }));
        if (!validContent(content)) throw Error('bounded_summary_required');
        for (const p of content.progress) {
          const pool = p.kind === 'user_statement' ? humans : memories;
          if (p.kind !== 'agent_judgment' && (!p.source_ids.length || p.source_ids.some(id => !pool.has(id))))
            throw Error('source_not_observed');
        }
        return JSON.stringify({ ok: true, kind: 'goldfish_session_guide/v1', staged: true, long_term_saved: false,
          guide: { revision: revision(state) + 1, session_id: String(agent.session.id), turn: active.turn,
            based_on_user_message_id: active.human, content } });
      } catch (error) {
        const safe = new Set(['live_root_required', 'stale_or_missing_human_turn', 'unsupported_field', 'bounded_summary_required', 'source_not_observed']);
        return JSON.stringify({ ok: false, error: safe.has(error.message) ? error.message : 'guide_unavailable',
          long_term_saved: false, hint: error.message === 'source_not_observed'
            ? 'Correct the source type and reference, then retry: current human statements use user_statement + current_user (or actual human message ID); retrieved records use memory_statement + exact memory_ref/original ID; your interpretation uses agent_judgment. Do not treat a memory ID as a current human ID.'
            : 'Continue the conversation. Refresh the current guide revision and cite observed sources; never save private reasoning.' });
      }
    } }));
}
