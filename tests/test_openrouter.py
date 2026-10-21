import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from ai_gateway import AIGateway
from openrouter_client import OpenRouterClient, OpenRouterError


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        with patch.dict('os.environ', {}, clear=True):
            return OpenRouterClient({'openrouter_api_key': 'test-key'})

    async def generate(self, handler):
        client = self.make_client()
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.counts = {}
        def count(key, by=1):
            self.counts[key] = self.counts.get(key, 0) + by
        gateway = AIGateway(client, {'provider': 'openrouter', 'min_interval_sec': 0}, count)
        with patch('openrouter_client.httpx.AsyncClient', return_value=http):
            return await gateway.generate('Vacancy', 'Use resume facts', 700)

    async def test_request_and_usage(self):
        def respond(request):
            self.assertEqual(str(request.url), 'https://openrouter.ai/api/v1/chat/completions')
            self.assertEqual(request.headers['authorization'], 'Bearer test-key')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'openrouter/free')
            self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'Use resume facts'})
            self.assertEqual(body['max_tokens'], 700)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': ' Letter '}}],
                'usage': {'prompt_tokens': 40, 'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': 5}}})
        self.assertEqual(await self.generate(respond), 'Letter')
        self.assertEqual(self.counts, {'ai_requests': 1, 'ai_prompt_token_count': 40,
                                     'ai_candidates_token_count': 20, 'ai_thoughts_token_count': 5})

    async def test_invalid_outputs(self):
        for choice in ([], [{'finish_reason': 'length', 'message': {'content': 'Partial'}}],
                       [{'finish_reason': 'stop', 'message': {'content': ''}}],
                       [{'finish_reason': 'content_filter', 'message': {'content': 'Partial'}}]):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                await self.generate(lambda request: httpx.Response(200, json={'choices': choice}))

    async def test_auth_error_not_retried_or_leaked(self):
        with self.assertRaises(OpenRouterError) as error:
            await self.generate(lambda request: httpx.Response(401, json={'error': {'message': 'secret response'}}))
        self.assertEqual(error.exception.code, 401)
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(self.counts['ai_requests'], 1)

    async def test_404_hint(self):
        cases = ((httpx.Response(404, text='Not found'), 'not_json'),
                 (httpx.Response(404, json={'error': {'message': 'No endpoints found matching your data policy'}}), 'data_policy'),
                 (httpx.Response(404, json={'error': {'message': 'No endpoints found for x'}}), 'model'))
        for response, hint in cases:
            with self.subTest(hint=hint), self.assertRaises(OpenRouterError) as error:
                await self.generate(lambda request, r=response: r)
            self.assertEqual(error.exception.hint, hint)
            self.assertNotIn('endpoints', str(error.exception))

    async def test_error_inside_success_response(self):
        with self.assertRaises(OpenRouterError) as error:
            await self.generate(lambda request: httpx.Response(200, json={'error': {'code': 403}}))
        self.assertEqual(error.exception.code, 403)

    async def test_retry_after_and_quota_stop(self):
        for delay, attempts in ((7, 3), (3600, 1)):
            client = self.make_client()
            client.generate = AsyncMock(side_effect=OpenRouterError(429, delay))
            gateway = AIGateway(client, {'provider': 'openrouter', 'min_interval_sec': 0}, lambda *args: None)
            with patch('ai_gateway.asyncio.sleep', new_callable=AsyncMock) as sleep, self.assertRaises(OpenRouterError):
                await gateway.generate('test')
            self.assertEqual(client.generate.await_count, attempts)
            if delay == 7:
                sleep.assert_awaited_with(7)
            else:
                sleep.assert_not_awaited()

    async def test_timeout_cancels_request(self):
        client = self.make_client()
        async def hang(*args):
            await asyncio.sleep(10)
        client.generate = hang
        gateway = AIGateway(client, {'provider': 'openrouter', 'timeout_sec': .01}, lambda *args: None)
        with self.assertRaises(asyncio.TimeoutError):
            await gateway.generate('test')

    def test_missing_key_environment_and_worker(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, 'OPENROUTER_API_KEY'):
            OpenRouterClient({})
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'env-key'}):
            client = OpenRouterClient({}, {'mode': 'cloudflare_worker', 'cloudflare_worker': {
                'base_url': 'https://worker.example/', 'proxy_token': 'proxy-secret'}})
        self.assertEqual(client.headers['Authorization'], 'Bearer env-key')
        self.assertEqual(client.headers['X-Proxy-Token'], 'proxy-secret')
        self.assertEqual(client.url, 'https://worker.example/openrouter/api/v1/chat/completions')

    def test_provider_model_is_independent_of_old_gemini_config(self):
        gateway = AIGateway(None, {'provider': 'openrouter', 'model': 'gemini-existing'}, lambda *args: None)
        self.assertEqual(gateway.model, 'openrouter/free')
        with self.assertRaises(ValueError):
            AIGateway(None, {'provider': 'typo'}, lambda *args: None)
