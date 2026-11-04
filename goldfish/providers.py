"""Selectable local Ollama and cloud/OpenAI-compatible model backends.

All endpoints are opt-in. No memory leaves the computer unless a non-local
endpoint has been explicitly configured. There are no third-party dependencies.
"""
from __future__ import annotations
import json
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOCAL_NAMES = {'localhost', '127.0.0.1', '::1'}


def _endpoint(endpoint: str, path: str) -> str:
    endpoint = endpoint.strip().rstrip('/')
    # Support both full endpoint and base URL; preserve backwards compatibility.
    if endpoint.endswith(path):
        return endpoint
    if not endpoint:
        raise ValueError('Model endpoint is not configured')
    if endpoint.endswith('/v1') and path.startswith('/v1/'):
        return endpoint + path[3:]
    return endpoint + path


def _check_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Model endpoint must be an http(s) URL')
    # API keys and personal memories should not travel over unencrypted WAN HTTP.
    if parsed.scheme == 'http' and parsed.hostname not in LOCAL_NAMES and os.getenv('GOLDFISH_ALLOW_INSECURE_REMOTE') != '1':
        raise ValueError('Remote model endpoints require HTTPS. Use GOLDFISH_ALLOW_INSECURE_REMOTE=1 only for a trusted private LAN.')


def _post_json(url, payload, key='', timeout=60):
    _check_url(url)
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = Request(url, headers=headers, data=data, method='POST')
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def _json_from_text(content):
    if not isinstance(content, str):
        raise ValueError('Model reply has no text content')
    s = content.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.I)
    # Some reasoning models prepend an internal-thought tag to final JSON.
    s = re.sub(r'^\s*<think>.*?</think>\s*', '', s, flags=re.S)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Restrict recovery to a single JSON object, never silently accept
        # arbitrary JSON fragments containing user-submitted instructions.
        start, end = s.find('{'), s.rfind('}')
        if start < 0 or end < start: raise
        obj = json.loads(s[start:end+1])
    if not isinstance(obj, dict):
        raise ValueError('Expected JSON object from model')
    return obj


@dataclass(frozen=True)
class ModelConfig:
    task: str  # 'LLM' or 'EMBED'
    provider: str
    endpoint: str
    model: str
    key: str = ''

    @property
    def configured(self):
        return bool(self.provider and self.endpoint and self.model)

    @property
    def identity(self):
        # Include endpoint, so swapping providers with same model name reindexes.
        return f'{self.provider}:{self.endpoint}:{self.model}'

    def public(self):
        """Status safe to show in diagnostics; API key is never included."""
        return {'provider': self.provider or 'disabled', 'model': self.model, 'configured': self.configured,
                'local': bool(self.endpoint and urlsplit(self.endpoint).hostname in LOCAL_NAMES)}


def load_config(task: str) -> ModelConfig:
    prefix = 'GOLDFISH_' + task.upper() + '_'
    provider = os.getenv(prefix + 'PROVIDER', '').strip().lower()
    endpoint = os.getenv(prefix + 'URL', '').strip()
    model = os.getenv(prefix + 'MODEL', '').strip()
    key = os.getenv(prefix + 'API_KEY', '').strip()
    # Legacy GOLDFISH_LLM_URL + MODEL works exactly as v0.1.
    if not provider and endpoint and model:
        provider = 'openai'
    if provider not in ('', 'openai', 'ollama'):
        raise ValueError(prefix+'PROVIDER must be openai or ollama')
    if provider == 'ollama' and not endpoint:
        endpoint = 'http://127.0.0.1:11434'
    return ModelConfig(task.upper(), provider, endpoint, model, key)


class ChatModel:
    def __init__(self, cfg: ModelConfig | None = None):
        self.cfg = cfg or load_config('LLM')

    @property
    def configured(self):
        return self.cfg.configured

    def extract(self, messages, related):
        if not self.configured:
            raise RuntimeError('Configure GOLDFISH_LLM_PROVIDER, GOLDFISH_LLM_MODEL and cloud URL if required')
        # Import here to keep the provider layer usable independently.
        from .consolidate import PROMPT
        inputs = [{'role': 'system', 'content': PROMPT},
                  {'role': 'user', 'content': json.dumps({'raw_messages': messages,
                      'candidate_existing_summaries': related}, ensure_ascii=False)}]
        if self.cfg.provider == 'ollama':
            url = _endpoint(self.cfg.endpoint, '/api/chat')
            data = _post_json(url, {'model': self.cfg.model, 'messages': inputs,
                                   'stream': False, 'format': 'json',
                                   'options': {'temperature': 0.1}}, self.cfg.key)
            return _json_from_text(data['message']['content'])
        url = _endpoint(self.cfg.endpoint, '/v1/chat/completions')
        payload = {'model': self.cfg.model, 'temperature': 0.1, 'messages': inputs}
        data = _post_json(url, payload, self.cfg.key)
        return _json_from_text(data['choices'][0]['message']['content'])


class EmbeddingModel:
    """Optional vector API. Does not require a second paid subscription."""
    def __init__(self, cfg: ModelConfig | None = None):
        self.cfg = cfg or load_config('EMBED')

    @property
    def configured(self):
        return self.cfg.configured

    @property
    def identity(self):
        return self.cfg.identity

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise RuntimeError('Embedding model not configured')
        if not texts:
            return []
        if self.cfg.provider == 'ollama':
            url = _endpoint(self.cfg.endpoint, '/api/embed')
            data = _post_json(url, {'model': self.cfg.model, 'input': texts}, self.cfg.key, timeout=45)
            vectors = data['embeddings']
        else:
            url = _endpoint(self.cfg.endpoint, '/v1/embeddings')
            data = _post_json(url, {'model': self.cfg.model, 'input': texts}, self.cfg.key, timeout=45)
            vectors = [item['embedding'] for item in sorted(data['data'], key=lambda x:x['index'])]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError('Embedding service returned an unexpected number of vectors')
        out=[]
        for vector in vectors:
            if not isinstance(vector,list) or not vector or len(vector)>8192:
                raise ValueError('Invalid embedding vector')
            v = [float(n) for n in vector]
            if not all(math.isfinite(n) for n in v):
                raise ValueError('Non-finite embedding vector')
            norm = math.sqrt(sum(n*n for n in v))
            if norm == 0: raise ValueError('Zero embedding vector')
            out.append([n/norm for n in v])
        return out
