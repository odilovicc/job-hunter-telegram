import asyncio
import json
import sqlite3
import tempfile
import unittest
import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_gateway import AIGateway
from hh_letters import LetterService, digest, plain_html, select_blocks, vacancy_id, fetch_vacancy
from hh_handlers import HHHandlers


class PreparationTests(unittest.TestCase):
    def test_only_expected_hh_urls(self):
        self.assertEqual(vacancy_id('https://hh.uz/vacancy/123?from=search#top'), '123')
        for url in ('https://hh.uz.evil.com/vacancy/123', 'http://hh.uz/vacancy/123',
                    'https://user@hh.uz/vacancy/123', 'https://hh.uz:8443/vacancy/123',
                    'https://127.0.0.1/vacancy/123', 'https://hh.uz/search/vacancy'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                vacancy_id(url)

    def test_html_cleaning_keeps_requirements_and_removes_scripts(self):
        self.assertEqual(plain_html('<p>Vue &amp; Nuxt</p><script>steal()</script><ul><li>Опыт</li><li>Опыт</li></ul>'),
                         'Vue & Nuxt\nОпыт')

    def test_matching_uses_aliases_and_preserves_whole_facts(self):
        self.assertEqual(select_blocks(['Python: other', 'Vue.js: shipped project'], 'Vue', 25),
                         ['Vue.js: shipped project'])
        self.assertEqual(select_blocks(['a' * 100], '', 10), [])


class HHFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_fixed_api_endpoint_and_normalized_fields(self):
        def respond(request):
            self.assertEqual(str(request.url), 'https://api.hh.ru/vacancies/123')
            self.assertEqual(request.headers['User-Agent'], 'TestBot/1.0')
            return httpx.Response(200, json={'name': 'Frontend', 'employer': {'name': 'Company'},
                'description': '<p>Vue required</p>', 'key_skills': [{'name': 'Vue'}],
                'branded_description': 'irrelevant huge page'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch('hh_letters.httpx.AsyncClient', return_value=client):
            result = await fetch_vacancy('123', 'TestBot/1.0')
        self.assertEqual(result['description'], 'Vue required')
        self.assertNotIn('branded_description', result)

    async def test_unavailable_hh_offers_text_input(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
        with patch('hh_letters.httpx.AsyncClient', return_value=client):
            with self.assertRaisesRegex(ValueError, 'Пришлите текст'):
                await fetch_vacancy('123', 'TestBot/1.0')


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'resume.pdf'
        self.path.write_text('resume-v1')
        self.conn = sqlite3.connect(':memory:')
        self.gateway = SimpleNamespace(model='test-model', generate=AsyncMock(return_value='Здравствуйте! Работаю с Vue.'))
        self.service = LetterService(self.conn, str(self.path), self.gateway, {})
        self.key = digest('resume-v1')
        self.conn.execute('INSERT INTO hh_profiles VALUES (?,?,1)',
                          (self.key, json.dumps(['Frontend developer', 'Vue.js project: improved rendering']),))
        self.conn.commit()
        self.vacancy = {'id': '123', 'name': 'Frontend Vue', 'company': 'Example',
                        'skills': ['Vue'], 'description': 'Требования: Vue. Обязанности: разрабатывать интерфейсы.'}

    async def asyncTearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    async def test_duplicate_requests_generate_once(self):
        job, _ = await self.service.create(self.vacancy)
        same, _ = await self.service.create(self.vacancy)
        self.assertEqual(job, same)
        results = await asyncio.gather(self.service.generate(job), self.service.generate(same))
        self.assertEqual([r[1] for r in results], [False, True])
        self.gateway.generate.assert_awaited_once()

    async def test_change_vacancy_or_model_invalidates_cache(self):
        job, _ = await self.service.create(self.vacancy)
        changed, _ = await self.service.create(dict(self.vacancy, description='Требования: Vue, PHP'))
        self.gateway.model = 'other-model'
        new_model, _ = await self.service.create(self.vacancy)
        self.assertEqual(len({job, changed, new_model}), 3)

    async def test_unapproved_resume_never_calls_ai(self):
        self.conn.execute('UPDATE hh_profiles SET approved=0')
        with self.assertRaises(ValueError):
            await self.service.create(self.vacancy)
        self.gateway.generate.assert_not_awaited()

    async def test_changed_resume_blocks_old_job(self):
        job, _ = await self.service.create(self.vacancy)
        self.path.write_text('resume-v2')
        self.conn.execute('INSERT INTO hh_profiles VALUES (?,?,1)', (digest('resume-v2'), '["New fact"]'))
        with self.assertRaises(ValueError):
            await self.service.generate(job)
        self.gateway.generate.assert_not_awaited()

    async def test_failed_job_is_durable_and_retryable(self):
        job, _ = await self.service.create(self.vacancy)
        self.gateway.generate.side_effect = RuntimeError('quota')
        with self.assertRaises(RuntimeError):
            await self.service.generate(job)
        self.assertEqual(self.service.get(job)['status'], 'failed')
        self.gateway.generate.side_effect = None
        await self.service.generate(job)
        self.assertEqual(self.service.get(job)['status'], 'done')

    async def test_restart_exposes_interrupted_jobs(self):
        job, _ = await self.service.create(self.vacancy)
        LetterService(self.conn, str(self.path), self.gateway, {})
        self.assertIn((job,), self.service.failed_jobs())

    async def test_variants_cache_per_parent(self):
        job, _ = await self.service.create(self.vacancy)
        await self.service.generate(job)
        parent = self.service.get(job)
        short, _ = await self.service.create(self.vacancy, 'short', parent)
        again, _ = await self.service.create(self.vacancy, 'short', parent)
        self.assertEqual(short, again)
        self.assertNotEqual(short, job)

    async def test_prompt_budget_prevents_request(self):
        self.service.config['max_prompt_chars'] = 10
        with self.assertRaises(ValueError):
            await self.service.create(self.vacancy)
        self.gateway.generate.assert_not_awaited()

    async def test_no_access_in_other_chats(self):
        handler = HHHandlers(self.service, 123)
        for user, chat in [(123, 42), (42, 123), (42, 42)]:
            update = SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat))
            self.assertFalse(handler.allowed(update))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_serialization_and_usage(self):
        active, maximum = 0, 0
        async def request(**kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            await asyncio.sleep(.001)
            active -= 1
            return SimpleNamespace(text='Letter', usage_metadata=SimpleNamespace(prompt_token_count=12,
                candidates_token_count=4, thoughts_token_count=2))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request)))
        counts = {}
        def counter(key, by=1):
            counts[key] = counts.get(key, 0) + by
        gateway = AIGateway(client, {'min_interval_sec': 0}, counter)
        await asyncio.gather(gateway.generate('monitor'), gateway.generate('hh'))
        self.assertEqual(maximum, 1)
        self.assertEqual(counts['ai_prompt_token_count'], 24)
        self.assertEqual(counts['ai_requests'], 2)

    async def test_timeout_cancels_request(self):
        cancelled = asyncio.Event()
        async def request(**kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request)))
        gateway = AIGateway(client, {'timeout_sec': .01, 'min_interval_sec': 0}, lambda *args: None)
        with self.assertRaises(asyncio.TimeoutError):
            await gateway.generate('hh')
        self.assertTrue(cancelled.is_set())

    async def test_retry_is_bounded(self):
        class QuotaError(Exception):
            code = 429
        request = AsyncMock(side_effect=QuotaError())
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request)))
        gateway = AIGateway(client, {'min_interval_sec': 0}, lambda *args: None)
        with patch('ai_gateway.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(QuotaError):
                await gateway.generate('hh')
        self.assertEqual(request.await_count, 3)


if __name__ == '__main__':
    unittest.main()
