"""Selectable local Ollama and cloud/OpenAI-compatible model backends.

All endpoints are opt-in. No memory leaves the computer unless a non-local
endpoint has been explicitly configured. There are no third-party dependencies.
"""
from __future__ import annotations
import json
import math
import os
import re
import time
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError('Model endpoint must not contain credentials or a fragment')
    # API keys and personal memories should not travel over unencrypted WAN HTTP.
    if parsed.scheme == 'http' and parsed.hostname not in LOCAL_NAMES and os.getenv('GOLDFISH_ALLOW_INSECURE_REMOTE') != '1':
        raise ValueError('Remote model endpoints require HTTPS. Use GOLDFISH_ALLOW_INSECURE_REMOTE=1 only for a trusted private LAN.')


class _NoModelRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is not explicit consent to send private text or API keys to
        # another endpoint. urllib otherwise preserves Authorization on 301/302.
        fp.close()
        raise ValueError('Model endpoint redirects are disabled; configure its final URL explicitly')


def _post_json(url, payload, key='', timeout=60):
    _check_url(url)
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = Request(url, headers=headers, data=data, method='POST')
    try:
        # Keep the standard proxy/CA handlers, but never follow redirects.
        with build_opener(_NoModelRedirects()).open(req, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as err:
        # Do not let server-supplied reason phrases or endpoint query credentials
        # enter the service's error logs or API responses.
        code = err.code
        err.close()
        raise RuntimeError(f'Model endpoint returned HTTP {code}') from None


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
    key: str = field(default='', repr=False)
    thinking: str = ''
    max_tokens: int | None = None

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
                'local': bool(self.endpoint and urlsplit(self.endpoint).hostname in LOCAL_NAMES),
                'thinking': self.thinking or 'provider_default', 'max_tokens': self.max_tokens}


def load_config(task: str) -> ModelConfig:
    prefix = 'GOLDFISH_' + task.upper() + '_'
    provider = os.getenv(prefix + 'PROVIDER', '').strip().lower()
    endpoint = os.getenv(prefix + 'URL', '').strip()
    model = os.getenv(prefix + 'MODEL', '').strip()
    key = os.getenv(prefix + 'API_KEY', '').strip()
    thinking = os.getenv(prefix + 'THINKING', '').strip().lower() if task.upper() == 'LLM' else ''
    if thinking not in ('', 'enabled', 'disabled'):
        raise ValueError(prefix + 'THINKING must be enabled or disabled')
    raw_limit = os.getenv(prefix + 'MAX_TOKENS', '').strip() if task.upper() == 'LLM' else ''
    max_tokens = int(raw_limit) if raw_limit else None
    if max_tokens is not None and max_tokens < 1:
        raise ValueError(prefix + 'MAX_TOKENS must be positive')
    # Legacy GOLDFISH_LLM_URL + MODEL works exactly as v0.1.
    if not provider and endpoint and model:
        provider = 'openai'
    if provider not in ('', 'openai', 'ollama'):
        raise ValueError(prefix+'PROVIDER must be openai or ollama')
    if provider == 'ollama' and not endpoint:
        endpoint = 'http://127.0.0.1:11434'
    return ModelConfig(task.upper(), provider, endpoint, model, key, thinking, max_tokens)


class ChatModel:
    def __init__(self, cfg: ModelConfig | None = None):
        self.cfg = cfg or load_config('LLM')
        self._measurements = threading.local()
        self.last_call = None

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
        return self.generate_json(inputs, purpose='consolidation')

    @property
    def last_call(self):
        return getattr(self._measurements, 'last_call', None)

    @last_call.setter
    def last_call(self, value):
        self._measurements.last_call = value

    def decide(self, prompt, data, *, purpose, timeout=6, max_tokens=4096):
        """Same explicit provider for semantic decisions; no hidden model route."""
        inputs = [{'role': 'system', 'content': prompt},
                  {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]
        output = self.generate_json(inputs, purpose=purpose, timeout=timeout, max_tokens=max_tokens)
        return output, dict(self.last_call or {})

    def generate_json(self, inputs, *, purpose, timeout=60, max_tokens=None):
        if not self.configured:
            raise RuntimeError('Model is not configured')
        output_limit = max_tokens if max_tokens is not None else self.cfg.max_tokens
        started = time.perf_counter()
        data = {}
        error = None
        try:
            if self.cfg.provider == 'ollama':
                url = _endpoint(self.cfg.endpoint, '/api/chat')
                payload = {'model': self.cfg.model, 'messages': inputs, 'stream': False,
                           'format': 'json', 'options': {'temperature': 0.1}}
                if self.cfg.thinking: payload['think'] = self.cfg.thinking == 'enabled'
                if output_limit: payload['options']['num_predict'] = output_limit
                data = _post_json(url, payload, self.cfg.key, timeout=timeout)
                return _json_from_text(data['message']['content'])
            url = _endpoint(self.cfg.endpoint, '/v1/chat/completions')
            payload = {'model': self.cfg.model, 'temperature': 0.1, 'messages': inputs}
            if self.cfg.thinking: payload['thinking'] = {'type': self.cfg.thinking}
            if output_limit: payload['max_tokens'] = output_limit
            data = _post_json(url, payload, self.cfg.key, timeout=timeout)
            return _json_from_text(data['choices'][0]['message']['content'])
        except Exception as err:
            error = type(err).__name__
            raise
        finally:
            # Measurement only: keep provider-reported counts, never prompts,
            # credentials, headers or response text in operational diagnostics.
            usage = data.get('usage') if isinstance(data, dict) else None
            if self.cfg.provider == 'ollama' and isinstance(data, dict):
                prompt_tokens, completion_tokens = data.get('prompt_eval_count'), data.get('eval_count')
                usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}
                if type(prompt_tokens) is int and type(completion_tokens) is int:
                    usage['total_tokens'] = prompt_tokens + completion_tokens
            self.last_call = {
                'provider': self.cfg.provider, 'model': data.get('model', self.cfg.model) if isinstance(data, dict) else self.cfg.model,
                'response_id': data.get('id') if isinstance(data, dict) else None,
                'finish_reason': (data.get('choices') or [{}])[0].get('finish_reason') if isinstance(data, dict) and self.cfg.provider != 'ollama' else data.get('done_reason') if isinstance(data, dict) else None,
                'latency_ms': round((time.perf_counter() - started) * 1000, 3),
                'usage': usage, 'error_type': error,
                'thinking': self.cfg.thinking or 'provider_default',
                'max_tokens': output_limit, 'purpose': purpose,
            }


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

    def embed(self, texts: list[str], *, timeout=45) -> list[list[float]]:
        if not self.configured:
            raise RuntimeError('Embedding model not configured')
        if not texts:
            return []
        if self.cfg.provider == 'ollama':
            url = _endpoint(self.cfg.endpoint, '/api/embed')
            data = _post_json(url, {'model': self.cfg.model, 'input': texts}, self.cfg.key, timeout=timeout)
            vectors = data['embeddings']
        else:
            url = _endpoint(self.cfg.endpoint, '/v1/embeddings')
            data = _post_json(url, {'model': self.cfg.model, 'input': texts}, self.cfg.key, timeout=timeout)
            items = data.get('data') if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(texts):
                raise ValueError('Embedding service returned an unexpected number of vectors')
            if any(not isinstance(item, dict) or type(item.get('index')) is not int
                   or 'embedding' not in item for item in items):
                raise ValueError('Embedding service returned invalid indices')
            if {item['index'] for item in items} != set(range(len(texts))):
                raise ValueError('Embedding service returned invalid indices')
            vectors = [item['embedding'] for item in sorted(items, key=lambda x:x['index'])]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError('Embedding service returned an unexpected number of vectors')
        out=[]
        dimension = None
        for vector in vectors:
            if not isinstance(vector,list) or not vector or len(vector)>8192:
                raise ValueError('Invalid embedding vector')
            if dimension is not None and len(vector) != dimension:
                raise ValueError('Embedding service returned inconsistent vector dimensions')
            dimension = len(vector)
            if any(type(n) not in (int, float) for n in vector):
                raise ValueError('Embedding vector must contain only numbers')
            v = [float(n) for n in vector]
            if not all(math.isfinite(n) for n in v):
                raise ValueError('Non-finite embedding vector')
            scale = max(abs(n) for n in v)
            if scale == 0: raise ValueError('Zero embedding vector')
            scaled = [n/scale for n in v]
            norm = math.hypot(*scaled)
            out.append([n/norm for n in scaled])
        return out
