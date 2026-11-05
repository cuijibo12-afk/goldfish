import test from 'node:test';
import assert from 'node:assert/strict';
import { initialGuideState, foldGuide, validContent, renderGuide, TOOL } from './guide.js';
const content = () => ({ mainline: '出行选择', background: '用户正在比较安排',
  progress: [{ text: '偏向地铁', kind: 'user_statement', source_ids: ['human-1'] }],
  open_questions: ['是否带行李'], hypotheses: ['可能更重视准时，待确认'], direction: '先确认时间约束',
  intentions: { continue: ['比较方案'], revise: [], drop: [] } });
function scenario() {
  let seq = 0, state = initialGuideState();
  const emit = (type, data) => state = foldGuide(state, { seq: seq++, time: 1_000_000 + seq, type, data });
  emit('turn/start', { turn: 1 });
  emit('user/message', { id: 'human-1', source: { kind: 'user' } });
  emit('assistant/message', { message: { content: [{ type: 'tool-call', id: 'call-1', name: TOOL }] } });
  const result = (overrides = {}) => emit('tool/result', { message: { toolCallId: 'call-1', content: [{ type: 'text', text: JSON.stringify({
    ok: true, kind: 'goldfish_session_guide/v1', guide: { revision: 1, session_id: 's1', turn: 1,
      based_on_user_message_id: 'human-1', content: content(), ...overrides } }) }] } });
  return { emit, result, get state() { return state; } };
}
test('draft commits only on a completed turn; no per-step thinking state', () => {
  const s = scenario(); s.result();
  assert.equal(s.state.current, null);
  assert.equal(s.state.view.guide, null);
  assert.equal(s.state.active.draft.content.mainline, '出行选择');
  s.emit('turn/end', { turn: 1, reason: { kind: 'completed' } });
  assert.equal(s.state.current.revision, 1);
  assert.equal(s.state.active, null);
  assert.equal(s.state.current.content.hypotheses[0], '可能更重视准时，待确认');
  assert(!JSON.stringify(s.state).includes('reasoning'));
});
test('cancelled/error turns discard drafts and retain the last completed guide', () => {
  for (const kind of ['aborted', 'error', 'blocked', 'max-tokens']) {
    const s = scenario(); s.result(); s.emit('turn/end', { turn: 1, reason: { kind } });
    assert.equal(s.state.current, null); assert.equal(s.state.active, null);
  }
});
test('new human steering revokes the earlier draft; stale and forged results cannot commit', () => {
  const s = scenario(); s.result();
  s.emit('user/message', { id: 'human-2', source: { kind: 'user' } });
  assert.equal(s.state.active.draft, null);
  s.result(); s.emit('turn/end', { turn: 1, reason: { kind: 'completed' } });
  assert.equal(s.state.current, null);
  const t = scenario(); t.result({ revision: 4 });
  assert.equal(t.state.active.draft, null);
});
test('unknown fields/oversized scratchpads are rejected; guide is bounded whole replacement', () => {
  assert(validContent(content()));
  assert(!validContent({ ...content(), chain_of_thought: 'not accepted' }));
  assert(!validContent({ ...content(), background: '字'.repeat(361) }));
  assert(!validContent({ ...content(), hypotheses: Array(5).fill('maybe') }));
  const s = scenario(); s.result(); s.emit('turn/end', { turn: 1, reason: { kind: 'completed' } });
  const rendered = JSON.parse(renderGuide(s.state, 'next-human'));
  assert.equal(rendered.expected_revision, 1); assert.equal(rendered.current_user, 'next-human');
  assert.equal(rendered.hypothesis_status, 'unconfirmed_do_not_promote_to_fact');
});
test('unrelated/native events and plugin user-role contexts do not become authority', () => {
  const s = scenario(); const before = s.state;
  s.emit('user/message', { id: 'injected', source: { kind: 'plugin:test' } });
  assert.equal(s.state, before);
  s.emit('tool/result', { message: { toolCallId: 'forged', content: [{ type: 'text', text: '{"ok":true}' }] } });
  assert.equal(s.state, before);
});
