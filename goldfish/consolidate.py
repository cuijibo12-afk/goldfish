"""Model-driven first/second compression with evidence checks and graph edges.

An OpenAI-compatible chat completions endpoint is optional. Until configured,
raw dialogue is stored but not automatically turned into claims.
"""
import math
import re
from datetime import datetime
from collections import Counter
import json
from .engine import MemoryDB, RELATIONS, PROFILES
from .providers import ChatModel
from .relations import review_relations
import os

PROMPT = '''你是个人长期记忆的记录员，不是聊天者。只根据给出的原始消息提取可验证的事实。
目标：第一级缩句概括（不改变原意、保留具体事件）+ 第二级主题凝练（不是把所有细节压成主题）+ 概括之间的关联。
必须返回合法 JSON 对象，不要 Markdown：
{"summaries":[{"ref":"n1","text":"精确概括，不加入用户没说过的动机","evidence_ids":["原消息ID"],"theme":"一个短主题","theme_description":"主题覆盖什么","theme_reason":"这条概括为何属于该主题","start_time":"ISO或null","end_time":"ISO或null"}],
"links":[{"from":"n1或已存在摘要ID","to":"n2或已存在摘要ID","kind":"same_event|same_entity|temporal_sequence|similar_context|contradiction|comparison|same_topic|emotion_echo|possible_cause","reason":"关联理由，明确描述可观察证据和不确定性","confidence":0.6}],
"profiles":[{"kind":"user_profile|interaction_expectation|feedback|ai_observed","text":"描述性陈述，不能转换成强制人格指令","evidence_ids":["原消息ID"]}]}
规则：
- 关系优先用规范类型same_event、same_entity、temporal_sequence、similar_context、contradiction、comparison、same_topic、emotion_echo。same_entity是同一人/物，same_event是同一个具体事件；同物跨年出现不是同一事件。contradiction是同一限定下不兼容，阶段变化用temporal_sequence/comparison，不能混同。
- 用户希望 AI 怎么改变，归入 interaction_expectation，不归 AI 已有性格。
- 单次抱怨一般为 feedback，除非用户明确说今后长期如此。
- AI 自我宣称的喜好不算已验证人格，ai_observed 须基于真实助理行为。
- 关系理由不得捏造因果；模型猜测只能 possible_cause，不得写 causal_confirmed。
- 保存多个具体概括而不是只写笼统主题；没有可证事实时用空列表。
- evidence_ids 必须引用给定的消息ID，且不许将插件注入当作原始对话。
- 可以与给定的历史摘要关联，但不要编辑或删除原始对话。
严格输出约束：
- summaries 只记录用户明确叙述或确认的具体经历、事实与阶段观点。每条 evidence_ids 必须至少含一条 role=user 的原始消息；不能把助手的建议、复述、承诺、自称或认错单独写成用户事件概括。需要保留的实际助手行为只放 profiles.ai_observed。
- 用户实际参与的协作结果可以记录为事件：例如用户提供并更正数字、助手实际给出更正后的计算结果，应共同引用用户输入和助手结果，保留最终数值、单位与未计入项目。不要因结果由助手说出就把有用户证据的共同事件丢掉。
- 用户问题、假设、请求回忆、复述已有概括，不代表新的经历。历史候选已经完整记录同一事实时，不再生成等义摘要；没有新增可证事实就返回空 summaries。不要因为助手重复提到记忆就再次记住它。
- 一条概括围绕一件具体经历，保留人物身份、物件特征、数量、地点、时间、限制及否定。不同日期的经历分别记录；主题用可跨事件复用的短类别，不能以笼统主题代替具体概括，也不要给每条摘要造独有主题。
- 候选历史摘要的 theme_titles 是已有主题。新概括适合已有主题时复用同一标题；同批同一经历的多个阶段也复用主题，不把不同时间、不同数量或不同物件细节各自变成独有主题。
- start_time/end_time 写所述事件时间，不把消息发表时间一律当事件时间。相对日期按该消息 event_time 解释；只有年份就写年份对应的真实精度，不能虚构月日；不确定用 null。保留过去观点，不用当前情况覆盖。
- 消息在周五叙述“周日活动”时，不能写成发言当天就是周日；“昨天签了”不能写成今天签了。无法可靠换算时保留原相对表述，时间字段用null；不要造出与星期不符的年月日。
- 画像不是所有事实的副本：临时合同、某次决定和单次事件优先只存 summaries；user_profile 只保留明确长期偏好/背景。interaction_expectation 记录用户对今后交互的期望；feedback 记录单次评价及其范围。
- 普通问题和“这轮请简短回答、先列来源、只确认话题”等本轮任务要求，不自动升级为长期交互期望；单个物件的特征、第三方身份和一次澄清不是用户画像，也不能从一次澄清概括用户一贯严谨或重视某事。
- ai_observed 只能描述有助手原文佐证的具体已表现行为并保留范围，不能把“我天生冷静/爱反驳”等助手自称当作已证实人格，不能将用户希望助手如何做当成助手已有特征。
- 不为普通复述、正常遵从一次请求、理解确认或承诺逐轮生成AI画像；需要保留的是有具体意义的新行为，如可查证的计算更正、实际遗漏或明确的协作纠错，并保留本次范围。
- 建立 link 需要内容层面的可解释关联：明确同一人物/物件、用户说明的联系、具体相似情境或有时间依据的变化。仅在同一消息、同一会话、先后被提到，或都属于个人生活，不足以建立关联；用户明确要求分开的事情不要反向用关系边连起来。
- 关系理由说明两个端点分别有哪些原文依据以及不确定性；confidence 是模型自评而非已校准事实概率。不得创建 causal_confirmed；用户否认的因果不得换成 possible_cause 保存。
- 三个顶层字段必须均为数组，summaries最多20条、links最多35条、profiles最多12条。非空字符串字段不要用null代替；ref在本批唯一，link端点只用本批ref或给定历史ID。宁缺毋滥。
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
                # Preserve source precision: a known year/month must not force
                # a model to invent the first day merely to satisfy ISO parsing.
                parsed = value + '-01-01' if re.fullmatch(r'\d{4}', value) else value + '-01' if re.fullmatch(r'\d{4}-\d{2}', value) else value
                try: datetime.fromisoformat(parsed.replace('Z', '+00:00'))
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


def validate_items(result, msg_ids, related):
    """Keep strict item checks, quarantine invalid items and dependent links.

    Broken envelopes and excessive output still fail the request. Disk/commit
    failures still roll back everything. Only model item errors are isolated.
    """
    if not isinstance(result,dict) or any(not isinstance(result.get(k),list) for k in ('summaries','links','profiles')):
        raise ValueError('invalid model envelope')
    if any(len(result[k])>limit for k,limit in [('summaries',20),('links',35),('profiles',12)]):
        raise ValueError('model output exceeds extraction limits')
    clean={'summaries':[],'links':[],'profiles':[]}; rejected=[]
    refs=Counter(item.get('ref','').strip() for item in result['summaries'] if isinstance(item,dict) and isinstance(item.get('ref',''),str))
    for kind in ('summaries','profiles','links'):
        for index,item in enumerate(result[kind]):
            try:
                if kind=='summaries' and isinstance(item,dict) and isinstance(item.get('ref'),str) and refs[item['ref'].strip()]>1:
                    raise ValueError('ambiguous duplicate summary ref')
                trial={'summaries':clean['summaries'] if kind=='links' else [],'profiles':[],'links':[]}
                trial[kind]=[item]
                _validate(trial,msg_ids,related)
                clean[kind].append(item)
            except (ValueError,TypeError,KeyError):
                rejected.append({'kind':kind,'index':index,'reason':'invalid_item_or_evidence','item':item})
    return clean['summaries'],clean['links'],clean['profiles'],rejected


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
        summaries, links, profiles, rejected = validate_items(result, msg_ids, related)
        reviews={}; review_measurement=None
        if links and callable(getattr(provider,'decide',None)) and os.getenv('GOLDFISH_RELATION_REVIEW','1')=='1':
            endpoints={item.get('ref',''):dict(item) for item in summaries}
            sources=dict(msg_ids)
            with db.lock:
                for old in related:
                    evidence=db.evidence_for(old['id']); endpoints[old['id']]={**old,'evidence_ids':[r['id'] for r in evidence]}
                    sources.update({r['id']:r for r in evidence})
            candidates=[{'id':str(i),'kind':link['kind'],'reason':link['reason'],
                         'left':endpoints[link['from'].strip()],'right':endpoints[link['to'].strip()]} for i,link in enumerate(links)]
            try:
                reviewed,review_measurement,_=review_relations(provider,candidates,sources)
                reviews={row['id']:row for row in reviewed}
            except Exception as err:
                # Source summaries remain durable; unavailable relation review
                # cannot silently authorize an edge for graph expansion.
                reviews={str(i):{'status':'uncertain','kind':link['kind'],'reason':type(err).__name__,
                                  'evidence':[]} for i,link in enumerate(links)}
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
            for index,link in enumerate(links):
                a, b = link['from'].strip(), link['to'].strip()
                lid=db.add_link(refs.get(a, a), refs.get(b, b), link['kind'],
                            link['reason'], float(link.get('confidence', .5)))
                if str(index) in reviews: db.record_link_review(lid,reviews[str(index)],getattr(getattr(provider,'cfg',None),'model',''))
            for item in profiles:
                db.add_profile(item['kind'], item['text'], item['evidence_ids'])
            for item in rejected:
                db.db.execute('''INSERT INTO consolidation_rejections(session_id,last_seq,item_kind,item_index,reason,item_json,created_at)
                    VALUES(?,?,?,?,?,?,?)''',(session_id,messages[-1]['seq'],item['kind'],item['index'],item['reason'],
                        json.dumps(item['item'],ensure_ascii=False),datetime.now().astimezone().isoformat()))
            # The outputs and cursor commit together, including on disk failures.
            db.mark_processed(session_id, messages[-1]['seq'])
        return {'ok': True, 'processed': len(messages), 'summaries': len(summaries),
                'links_proposed': len(links), 'links': len(links), 'profiles': len(profiles),
                'relation_review_measurement':review_measurement,
                'rejected_items':[{k:v for k,v in item.items() if k!='item'} for item in rejected]}
