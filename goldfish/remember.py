"""Explicit literal memories. No extraction model, inferred claims, or cursor advance."""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from .engine import now_iso


def unquoted(text):
    # Keep offsets stable: a directive quoted in a document/example is data.
    pattern = r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|(?m:^\s*>[^\n]*)'
    return re.sub(pattern, lambda m: ' ' * len(m[0]), text)


def explicit_request(text, request):
    if not isinstance(request, str) or not request.strip() or len(request) > 2000:
        return False
    plain = unquoted(text)
    if request not in plain:
        return False
    if re.search(r'(?:不要|不用|无需|不必|禁止|别)\s*(?:再|实际|真的|自动|帮我|替我|给我|把它|把这条|把这个|进行|做)?\s*(?:记住|记下|记忆|保存|存|写入|记录)|示例|例如|举例|演示|记住了?[吗么没]|记住什么|\bexample\b|(?:do\s+not|don.t|never)\s+(?:actually\s+)?(?:remember|save|store|record)', plain, re.I):
        return False
    directive = r'(?:^|[。！？!?；;\n])\s*(?:(?:请你|请|麻烦|帮我|替我|现在|务必|我希望你)\s*)*(?:记住|记下|记到长期记忆|保存为记忆|存入长期记忆)|(?:^|[.!?;\n])\s*(?:please\s+)?(?:remember\b|save\b.{0,40}\bmemory\b)'
    # The supplied excerpt must overlap a real imperative, not just the same
    # verb mentioned elsewhere in a question or explanation.
    return any(request in plain[m.start():] and plain.find(request, m.start()) < m.end()
               for m in re.finditer(directive, plain, re.I))


def write_key(path):
    """Local capability shared only by host and service; never put it in config."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(os.urandom(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('invalid_remember_key_file')
    return key


def verify(body, signature, key):
    if not key or not isinstance(signature, str) or not hmac.compare_digest(
            hmac.new(key, body, hashlib.sha256).hexdigest(), signature):
        raise PermissionError('remember_signature_required')
    data = json.loads(body)
    issued = data.get('issued_at') if isinstance(data, dict) else None
    if type(issued) not in (float, int) or not abs(time.time() - issued) <= 120:
        raise PermissionError('remember_authorization_expired')
    return data


def _source(value):
    if not isinstance(value, dict) or value.get('role') != 'user' or value.get('source_kind') != 'user':
        raise ValueError('real_user_source_required')
    for key in ('namespace', 'session_id', 'message_id', 'event_time', 'text'):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key]) > (200000 if key == 'text' else 200):
            raise ValueError('invalid_source_metadata')
    if type(value.get('event_seq')) is not int or value['event_seq'] < 0:
        raise ValueError('invalid_source_sequence')
    datetime.fromisoformat(value['event_time'].replace('Z', '+00:00'))
    return value


def source_id(source):
    identity = [source[k] for k in ('namespace', 'session_id', 'event_seq', 'message_id')]
    return 'dsh:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def save(db, data):
    if data.get('version') != 1:
        raise ValueError('invalid_remember_version')
    auth, source = _source(data.get('authorization')), _source(data.get('source'))
    if any(auth[k] != source[k] for k in ('namespace', 'session_id')):
        raise ValueError('cross_session_source')
    quote, request = data.get('quote'), data.get('request_quote')
    if not isinstance(quote, str) or not quote.strip() or quote != quote.strip() or len(quote) > 2000 or quote not in source['text']:
        raise ValueError('literal_source_quote_required')
    if not explicit_request(auth['text'], request):
        raise ValueError('explicit_user_request_required')
    same = source_id(auth) == source_id(source)
    if not same:
        if source['event_seq'] >= auth['event_seq'] or data.get('previous_user_message_id') != source['message_id']:
            raise ValueError('source_not_authorized')
        if not re.search(r'(?:刚才|上一(?:条|句)|前一(?:条|句))|\b(?:previous|last)\s+(?:message|statement)\b', request, re.I):
            raise ValueError('repeat_quote_in_current_request')
    mid, aid = source_id(source), source_id(auth)
    claim = hashlib.sha256((mid + '\0' + quote).encode()).hexdigest()
    receipt = 'receipt:' + hashlib.sha256((claim + '\0' + aid).encode()).hexdigest()
    with db.transaction():
        # Immutable source identity catches a compromised/mistaken caller that
        # tries reusing an event locator with changed content.
        for item, ident in ((source, mid), (auth, aid)):
            db.add_message(item['namespace'] + ':' + item['session_id'], 'user', item['text'],
                           item['event_time'], ident, 'human')
        old = db.db.execute('SELECT summary_id FROM explicit_memory_claims WHERE claim_key=?', (claim,)).fetchone()
        duplicate = old is not None
        memory = old[0] if old else 'remember:' + claim
        if not old:
            db.add_summary(quote, [mid, aid], confidence=1, summary_id=memory)
            db.db.execute('INSERT INTO explicit_memory_claims VALUES(?,?,?,?,?)',
                          (claim, memory, mid, quote, now_iso()))
        prior = db.db.execute('SELECT created_at FROM explicit_memory_receipts WHERE receipt_id=?', (receipt,)).fetchone()
        saved_at = prior[0] if prior else now_iso()
        if not prior:
            db.db.execute('INSERT INTO explicit_memory_receipts VALUES(?,?,?,?,?)',
                          (receipt, claim, aid, request, saved_at))
            db.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?)', (memory, aid))
    return {'ok': True, 'saved': True, 'memory_id': memory, 'receipt_id': receipt,
            'duplicate': duplicate, 'replayed_receipt': prior is not None,
            'kind': 'explicit_user_statement', 'text': quote, 'saved_at': saved_at,
            'verification': 'literal_user_quote_not_independently_verified',
            'records': [{'id': memory, 'kind': 'explicit_user_statement', 'text': quote,
                         'provenance': 'user_requested_quote', 'source_message_id': mid,
                         'authorization_message_id': aid}], 'next_cursor': None, 'degraded': False}
