"""Model-driven first/second compression with evidence checks and graph edges.

An OpenAI-compatible chat completions endpoint is optional. Until configured,
raw dialogue is stored but not automatically turned into claims.
"""
from .engine import MemoryDB
from .providers import ChatModel, load_config

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

def consolidate_session(db: MemoryDB, session_id: str, provider=None):
    provider=provider or ChatModel()
    if isinstance(provider,ChatModel) and not provider.configured:
        return {'ok':False,'reason':'model_not_configured','processed':0}
    messages=db.get_unprocessed_messages(session_id)
    if len(messages)<2: return {'ok':True,'reason':'not_enough_messages','processed':0}
    msg_ids={m['id']:m for m in messages}
    related=db.recent_summaries_for_extraction(' '.join(m['text'] for m in messages if m['role']=='user'))
    result=provider.extract(messages,related)
    if not isinstance(result,dict): raise ValueError('model output should be JSON object')
    raw_summaries=result.get('summaries',[])
    raw_links=result.get('links',[])
    raw_profiles=result.get('profiles',[])
    if not all(isinstance(x,list) for x in [raw_summaries,raw_links,raw_profiles]):
        raise ValueError('invalid output arrays')
    # Validate all raw evidence *before* any database write, avoiding partial unsafe writes.
    for item in raw_summaries+raw_profiles:
        if not isinstance(item,dict): raise ValueError('invalid item')
        ids=item.get('evidence_ids',[])
        if not isinstance(ids,list) or not ids or any(mid not in msg_ids for mid in ids):
            raise ValueError('missing/invalid evidence IDs')
    refs={}
    with db.db:  # existing DB helpers commit; validation above prevents most incomplete writes.
        for item in raw_summaries[:20]:
            # Require a user source for personal user facts: assistant text is not user evidence.
            ids=item['evidence_ids']
            if not any(msg_ids[mid]['role']=='user' for mid in ids): continue
            text=str(item.get('text','')).strip()
            if not text: continue
            sid=db.add_summary(text,ids,item.get('start_time'),item.get('end_time'))
            if item.get('ref'): refs[str(item['ref'])]=sid
            if item.get('theme'):
                tid=db.add_theme(str(item['theme']),str(item.get('theme_description','')))
                db.attach_theme(tid,sid,str(item.get('theme_reason','')))
        allowed_old={x['id'] for x in related}
        for link in raw_links[:35]:
            if not isinstance(link,dict): continue
            a=refs.get(str(link.get('from')),str(link.get('from')))
            b=refs.get(str(link.get('to')),str(link.get('to')))
            if a not in (set(refs.values())|allowed_old) or b not in (set(refs.values())|allowed_old): continue
            try:
                db.add_link(a,b,str(link.get('kind')),str(link.get('reason','')),float(link.get('confidence',.5)))
            except (ValueError,TypeError): continue
        for item in raw_profiles[:12]:
            kind=str(item.get('kind',''))
            if kind not in {'user_profile','interaction_expectation','feedback','ai_observed'}:continue
            try: db.add_profile(kind,str(item.get('text','')),item['evidence_ids'])
            except ValueError: continue
        db.mark_processed(session_id,messages[-1]['seq'])
    return {'ok':True,'processed':len(messages),'summaries':len(refs),'links_proposed':len(raw_links)}
