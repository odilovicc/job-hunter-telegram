"""OpenRouter HTTP adapter. No SDK dependency and no response bodies in error logs."""
import os
from types import SimpleNamespace

import httpx


def classify_error(response):
    """Коротко называет причину 401/403/404, не отдавая наружу текст ответа.

    - not_json: ответил не OpenRouter (Worker без маршрута /openrouter/ или с неверным
      proxy_token — он отвечает текстом «Forbidden»/«Not found», не тот URL);
    - data_policy: настройки приватности OpenRouter запрещают бесплатные модели;
    - model: OpenRouter не нашёл модель или эндпоинт для неё (например, embedding-модель)."""
    try:
        message = str((response.json().get('error') or {}).get('message', '')).lower()
    except (ValueError, AttributeError):
        return 'not_json'
    if response.status_code != 404:
        return 'openrouter'
    return 'data_policy' if 'data policy' in message or 'privacy' in message else 'model'


class OpenRouterError(Exception):
    def __init__(self, code, retry_after=None, hint=None):
        self.code = code
        self.retry_after = retry_after
        self.hint = hint
        super().__init__(f'OpenRouter API error: HTTP {code}')


class OpenRouterClient:
    def __init__(self, config, network=None):
        self.api_key = (os.environ.get('OPENROUTER_API_KEY') or config.get('openrouter_api_key') or '').strip()
        if not self.api_key:
            raise ValueError('Задайте OPENROUTER_API_KEY или ai.openrouter_api_key в config.yaml.')
        self.url = 'https://openrouter.ai/api/v1/chat/completions'
        self.headers = {'Authorization': f'Bearer {self.api_key}'}
        if (network or {}).get('mode') == 'cloudflare_worker':
            worker = network['cloudflare_worker']
            self.url = worker['base_url'].rstrip('/') + '/openrouter/api/v1/chat/completions'
            self.headers['X-Proxy-Token'] = worker['proxy_token']
        self.timeout = float(config.get('timeout_sec', 45))

    async def generate(self, model, prompt, system, max_output_tokens):
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, headers=self.headers, json={
                'model': model, 'messages': messages, 'max_tokens': max_output_tokens,
                'stream': False,
            })
        if response.is_error:
            try:
                retry_after = float(response.headers.get('retry-after', '0'))
            except ValueError:
                retry_after = 0
            raise OpenRouterError(response.status_code, retry_after,
                                  classify_error(response) if response.status_code in (401, 403, 404) else None)
        data = response.json()
        if data.get('error'):
            try:
                code = int(data['error'].get('code', 502))
            except (TypeError, ValueError):
                code = 502
            raise OpenRouterError(code)
        choices = data.get('choices') or []
        if not choices:
            raise ValueError('OpenRouter вернул ответ без вариантов письма.')
        choice = choices[0]
        if choice.get('finish_reason') not in ('stop', 'length'):
            raise ValueError('OpenRouter не завершил текст письма.')
        usage = data.get('usage') or {}
        content = (choice.get('message') or {}).get('content')
        if content is not None and not isinstance(content, str):
            raise ValueError('OpenRouter вернул неподдерживаемый формат письма.')
        return SimpleNamespace(
            text=content,
            candidates=[SimpleNamespace(finish_reason='MAX_TOKENS' if choice.get('finish_reason') == 'length' else 'STOP')],
            usage_metadata=SimpleNamespace(
                prompt_token_count=usage.get('prompt_tokens'),
                candidates_token_count=usage.get('completion_tokens'),
                thoughts_token_count=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens'),
            ),
        )
