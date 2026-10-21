import ast
import asyncio
import logging
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from history_scan import HistoryScanner, MessageGate


class ScanTests(unittest.IsolatedAsyncioTestCase):
    def setup_scanner(self, items, channels=('one',)):
        async def iterate(channel, **kwargs):
            if channel == 'broken':
                raise RuntimeError('no access')
            for item in items:
                yield item
        self.process = AsyncMock(return_value='delivered')
        self.scanner = HistoryScanner(SimpleNamespace(iter_messages=iterate), channels, self.process, 123)
        self.scanner.end_time = datetime.now(timezone.utc)
        self.scanner.start_time = self.scanner.end_time - timedelta(days=3)
        self.message = SimpleNamespace(reply_text=AsyncMock(), get_bot=lambda: 'bot')
        return self.scanner

    async def test_time_window_including_lower_boundary(self):
        scanner = self.setup_scanner([])
        dates = [scanner.end_time + timedelta(seconds=1), scanner.end_time - timedelta(hours=2),
                 scanner.start_time, scanner.start_time - timedelta(seconds=1)]
        async def iterate(channel, **kwargs):
            self.assertEqual(kwargs['offset_date'], scanner.end_time)
            for i, date in enumerate(dates):
                yield SimpleNamespace(id=i, date=date)
        scanner.client.iter_messages = iterate
        await scanner.run(self.message)
        self.assertEqual([c.args[0].id for c in self.process.await_args_list], [1, 2])
        self.assertEqual(scanner.counts['delivered'], 2)

    async def test_channel_failure_does_not_abort_other_channels(self):
        scanner = self.setup_scanner([], ('broken', 'one'))
        await scanner.run(self.message)
        self.assertEqual(scanner.error_channels, ['broken'])
        self.assertEqual(scanner.completed, 2)

    async def test_stop_after_current_message(self):
        now = datetime.now(timezone.utc)
        scanner = self.setup_scanner([SimpleNamespace(id=i, date=now) for i in range(3)])
        async def process(*args):
            scanner.stop_requested = True
            return 'delivered'
        scanner.process = AsyncMock(side_effect=process)
        await scanner.run(self.message)
        scanner.process.assert_awaited_once()
        self.assertIn('остановлено', self.message.reply_text.await_args.args[0])

    async def test_only_one_background_scan(self):
        scanner = self.setup_scanner([])
        blocker = asyncio.Event()
        scanner.run = AsyncMock(side_effect=lambda *args: None)
        async def run(*args):
            await blocker.wait()
        scanner.run = run
        app = SimpleNamespace(create_task=lambda coro, **kw: asyncio.create_task(coro))
        context = SimpleNamespace(application=app)
        await scanner.start(self.message, context, 3)
        task = scanner.task
        await scanner.start(self.message, context, 7)
        self.assertIs(scanner.task, task)
        self.assertEqual(scanner.end_time - scanner.start_time, timedelta(days=3))
        blocker.set()
        await scanner.shutdown()

    async def test_private_access_and_invalid_period(self):
        scanner = self.setup_scanner([])
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                                 effective_chat=SimpleNamespace(id=999), effective_message=self.message)
        await scanner.menu(update, SimpleNamespace(args=['5'], user_data={}))
        self.message.reply_text.assert_not_awaited()
        update.effective_chat.id = 123
        for value in ['0', '-3', '366', 'hello']:
            await scanner.menu(update, SimpleNamespace(args=[value], user_data={}))
            self.assertIn('Укажите число', self.message.reply_text.await_args.args[0])
        self.assertIsNone(scanner.task)

    async def test_results_are_saved_until_completion_and_reviewed_one_at_a_time(self):
        now = datetime.now(timezone.utc)
        scanner = self.setup_scanner([SimpleNamespace(id=i, date=now) for i in range(2)])
        bot = SimpleNamespace(send_message=AsyncMock())
        self.message.get_bot = lambda: bot
        async def collect(item, delivery):
            bot.send_message.assert_not_awaited()
            await delivery.send_message(chat_id=123, text=f'Vacancy {item.id}',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Send', callback_data=f'send_2_{item.id}')]]))
            return 'delivered'
        scanner.process = collect
        await scanner.run(self.message)
        bot.send_message.assert_not_awaited()
        first = self.message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        await scanner.show(self.message, int(first.split(':')[-1]))
        bot.send_message.assert_awaited_once()
        card = bot.send_message.await_args.kwargs
        self.assertEqual(card['text'], 'Vacancy 0')
        self.assertEqual(card['reply_markup'].inline_keyboard[0][0].callback_data, 'send_2_0')
        next_id = int(card['reply_markup'].inline_keyboard[-1][0].callback_data.split(':')[-1])
        await scanner.show(self.message, next_id)
        card = bot.send_message.await_args.kwargs
        self.assertEqual(card['text'], 'Vacancy 1')
        self.assertEqual(card['reply_markup'].inline_keyboard[-1][0].callback_data, 'scan:results')

    async def test_batches_survive_restart_and_navigation_stays_in_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'scan.db')
            conn = sqlite3.connect(path)
            scanner = HistoryScanner(None, [], None, 123, conn=conn)
            scanner.run_id = 1
            await scanner.send_message(chat_id=123, text='First scan')
            scanner.run_id = 2
            await scanner.send_message(chat_id=123, text='Second scan')
            conn.close()
            conn = sqlite3.connect(path)
            restarted = HistoryScanner(None, [], None, 123, conn=conn)
            bot = SimpleNamespace(send_message=AsyncMock())
            message = SimpleNamespace(get_bot=lambda: bot, reply_text=AsyncMock())
            await restarted.show(message, 1)
            card = bot.send_message.await_args.kwargs
            self.assertEqual(card['text'], 'First scan')
            self.assertEqual(card['reply_markup'].inline_keyboard[-1][0].callback_data, 'scan:results')
            conn.close()

    async def test_cannot_open_current_batch_but_can_open_previous_batch(self):
        scanner = self.setup_scanner([])
        scanner.run_id = 1
        await scanner.send_message(chat_id=123, text='Previous')
        scanner.run_id = 2
        await scanner.send_message(chat_id=123, text='Current')
        scanner.task = Mock(done=lambda: False)
        bot = SimpleNamespace(send_message=AsyncMock())
        self.message.get_bot = lambda: bot
        await scanner.show(self.message, 2)
        bot.send_message.assert_not_awaited()
        await scanner.show(self.message, 1)
        self.assertEqual(bot.send_message.await_args.kwargs['text'], 'Previous')

    async def test_send_and_skip_keep_scan_navigation(self):
        tree = ast.parse(Path('main.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'safe_edit')
        env = dict(InlineKeyboardMarkup=InlineKeyboardMarkup, ParseMode=ParseMode,
                   TelegramError=RuntimeError, log=logging.getLogger('test'))
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), env)
        query = SimpleNamespace(edit_message_text=AsyncMock(), message=SimpleNamespace(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('Send', callback_data='send_1_2')],
            [InlineKeyboardButton('Next', callback_data='scan:view:3')]])))
        await env['safe_edit'](query, 'Skipped')
        rows = query.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].callback_data, 'scan:view:3')


class ProcessingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load only the handlers: importing main would open real DB/config/network clients.
        tree = ast.parse(Path('main.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                     and n.name in ('handle_message', '_handle_message')]
        self.processed = set()
        self.db = Mock()
        self.db.is_processed.side_effect = lambda mid, cid: (mid, cid) in self.processed
        self.db.mark_processed.side_effect = lambda mid, cid: self.processed.add((mid, cid))
        self.send = AsyncMock()
        self.generate = AsyncMock(return_value=('letter', False))
        self.env = dict(Bot=object, _message_gate=MessageGate(), db=self.db,
            log=logging.getLogger('test'), config={'filters': {'level_2_scoring': {'min_pass_score': 5}}},
            level_1_filter=Mock(return_value=True), level_2_scoring=Mock(return_value=(6, True, [])),
            channel_username_by_chat_id={}, extract_contact=lambda text: None,
            generate_letter=self.generate, send_vacancy_to_admin=self.send)
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'main.py', 'exec'), self.env)
        self.message = SimpleNamespace(id=1, chat_id=2, text='Vue vacancy')

    async def test_live_and_scan_deliver_only_once(self):
        action = self.env['handle_message']
        outcomes = await asyncio.gather(action(self.message, None), action(self.message, None))
        self.assertEqual(outcomes, ['delivered', 'duplicate'])
        self.generate.assert_awaited_once()
        self.send.assert_awaited_once()
        self.assertFalse(self.env['_message_gate'].entries)

    async def test_failed_delivery_can_be_retried(self):
        self.send.side_effect = RuntimeError('offline')
        self.assertEqual(await self.env['handle_message'](self.message, None), 'failed')
        self.assertFalse(self.processed)
        self.send.side_effect = None
        self.assertEqual(await self.env['handle_message'](self.message, None), 'delivered')

    async def test_rejections_are_not_recounted(self):
        self.env['level_1_filter'].return_value = False
        action = self.env['handle_message']
        self.assertEqual(await action(self.message, None), 'rejected')
        self.assertEqual(await action(self.message, None), 'duplicate')
        self.db.increment_counter.assert_called_once_with('level1_rejected')
        self.generate.assert_not_awaited()
