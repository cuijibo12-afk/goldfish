"""Model-driven first/second compression with evidence checks and graph edges.

An OpenAI-compatible chat completions endpoint is optional. Until configured,
raw dialogue is stored but not automatically turned into claims.
"""
import math
from datetime import datetime
from .engine import MemoryDB, RELATIONS, PROFILES
from .providers import ChatModel

PROMPT = '''你是个人长期记忆的记录员，不是聊天者。只根据给出的原始消息提取可验证的事实。
目标：第一级缩句概括（不改变原意、保留具体事件）+ 第二级主题凝练（不是把所有细节压成主题）+ 概括之间的关联。
必须返回合法 JSON 对象，不要 Markdown：
{"summaries":[{"ref":"n1","text":"精确概括，不加入用户没说过的动机","evidence_ids":["原消息ID"],"theme":"一个短主题","theme_description":"主题覆盖什么","theme_reason":"这条概括为何属于该主题","start_time":"ISO或null","end_time":"ISO或null"}],
"links":[{"from":"n1或已存在摘要ID","to":"n2或已存在摘要ID","kind":"same_topic|same_person|same_event|chronological|contrast|emotion_echo|contextual|possible_cause","reason":"关联理由，明确描述可观察证据和不确定性","confidence":0.6}],
"profiles":[{"kind":"user_profile|interaction_expectation|feedback|ai_observed","text":"描述性陈述，不能转换成强制人格指令","evidence_ids":["原消息ID"]}]}
规则：
- 用户希望 AI 怎么改变，归入 interaction_expectation，不归 AI 已有性格。
- 单次抱怨一般为 feedback，除非用户明确说今后长期如此。
- AI 自我宣称的喜好不算已验证人格，ai_observed 须基于真实助理行为。
- 关系理由不得捏造因果；模型猜测只能 possible_cause，不得写 causal_confirmed。
- 保存多个具体概括而不是只写笼统主题；没有可证事实时用空列表。
- evidence_ids 必须引用给定的消息ID，且不许将插件注入当作原始对话。
- 可以与给定的历史摘要关联，但不要编辑或删除原始对话。
'''

class OpenAICompatible(ChatModel):
    """Backwards-compatible name: now supports Ollama and OpenAI-format APIs."""
    pass

def _text(item, key, required=False):
    value = item.get(key, '')
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError('invalid model text field')
    return value.strip()


def _validate(result, msg_ids, related):
    if not isinstance(result, dict):
        raise ValueError('model output should be JSON object')
    arrays = [result.get(key) for key in ('summaries', 'links', 'profiles')]
    if not all(isinstance(value, list) for value in arrays):
        raise ValueError('invalid output arrays')
    summaries, links, profiles = arrays
    if len(summaries) > 20 or len(links) > 35 or len(profiles) > 12:
        raise ValueError('model output exceeds extraction limits')
    refs = set()
    for item in summaries + profiles:
        if not isinstance(item, dict): raise ValueError('invalid model item')
        _text(item, 'text', required=True)
        ids = item.get('evidence_ids')
        if (not isinstance(ids, list) or not ids or
                any(not isinstance(mid, str) or mid not in msg_ids for mid in ids)):
            raise ValueError('missing/invalid evidence IDs')
    for item in summaries:
        if not any(msg_ids[mid]['role'] == 'user' for mid in item['evidence_ids']):
            raise ValueError('summary requires actual user evidence')
        ref = _text(item, 'ref')
        if ref:
            if ref in refs or ref in {row['id'] for row in related}:
                raise ValueError('duplicate or ambiguous summary ref')
            refs.add(ref)
        for key in ('theme', 'theme_description', 'theme_reason'):
            _text(item, key)
        for key in ('start_time', 'end_time'):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str): raise ValueError('invalid event time')
                try: datetime.fromisoformat(value.replace('Z', '+00:00'))
                except ValueError: raise ValueError('invalid event time') from None
    allowed = refs | {row['id'] for row in related}
    for link in links:
        if not isinstance(link, dict): raise ValueError('invalid model link')
        a, b = _text(link, 'from', True), _text(link, 'to', True)
        if a not in allowed or b not in allowed or a == b:
            raise ValueError('invalid model link endpoints')
        if link.get('kind') not in RELATIONS - {'causal_confirmed'}:
            raise ValueError('invalid model relation')
        _text(link, 'reason', True)
        try: confidence = float(link.get('confidence', .5))
        except (TypeError, ValueError): raise ValueError('invalid relation confidence') from None
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('invalid relation confidence')
    for item in profiles:
        kind = item.get('kind')
        if kind not in PROFILES: raise ValueError('invalid profile kind')
        role = 'assistant' if kind == 'ai_observed' else 'user'
        if not any(msg_ids[mid]['role'] == role for mid in item['evidence_ids']):
            raise ValueError('profile requires matching role evidence')
    return summaries, links, profiles


def consolidate_session(db: MemoryDB, session_id: str, provider=None):
    provider = provider or ChatModel()
    if isinstance(provider, ChatModel) and not provider.configured:
        return {'ok': False, 'reason': 'model_not_configured', 'processed': 0}
    # Only this session is serialized during inference. Ingest/recall and other
    # sessions remain available while an optional local/cloud model is slow.
    with db.session_lock(session_id):
        with db.lock:
            messages = db.get_unprocessed_messages(session_id)
            if not messages:
                return {'ok': True, 'reason': 'no_messages', 'processed': 0}
            related = db.recent_summaries_for_extraction(
                ' '.join(m['text'] for m in messages if m['role'] == 'user'))
        result = provider.extract(messages, related)
        msg_ids = {m['id']: m for m in messages}
        summaries, links, profiles = _validate(result, msg_ids, related)
        refs = {}
        with db.transaction():
            current = db.get_unprocessed_messages(session_id, limit=1)
            if not current or current[0]['id'] != messages[0]['id']:
                # Another process may have completed this batch while the model
                # was running. Never commit its outputs a second time.
                return {'ok': True, 'reason': 'stale_snapshot', 'processed': 0}
            for item in summaries:
                sid = db.add_summary(item['text'], item['evidence_ids'],
                                     item.get('start_time'), item.get('end_time'))
                if item.get('ref'): refs[item['ref'].strip()] = sid
                if item.get('theme', '').strip():
                    tid = db.add_theme(item['theme'], item.get('theme_description', ''))
                    db.attach_theme(tid, sid, item.get('theme_reason', ''))
            for link in links:
                a, b = link['from'].strip(), link['to'].strip()
                db.add_link(refs.get(a, a), refs.get(b, b), link['kind'],
                            link['reason'], float(link.get('confidence', .5)))
            for item in profiles:
                db.add_profile(item['kind'], item['text'], item['evidence_ids'])
            # The outputs and cursor commit together, including on disk failures.
            db.mark_processed(session_id, messages[-1]['seq'])
        return {'ok': True, 'processed': len(messages), 'summaries': len(summaries),
                'links_proposed': len(links), 'links': len(links), 'profiles': len(profiles)}
