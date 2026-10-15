"""Exercise the application factory without starting main.py's live clients."""
import ast
import unittest
from pathlib import Path

from telegram.ext import Application
from telegram.request import HTTPXRequest


class FileRoutingTests(unittest.TestCase):
    def test_worker_routes_api_and_file_downloads(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text())
        factory = next(node for node in source.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'build_ptb_application')
        namespace = dict(Application=Application, HTTPXRequest=HTTPXRequest,
                         ADMIN_BOT_TOKEN='123:test', ptb_base_url='https://worker.example/telegram-bot/bot',
                         ptb_base_file_url='https://worker.example/telegram-bot/file/bot',
                         ptb_proxy_headers={'X-Proxy-Token': 'test-proxy-token'})
        exec(compile(ast.Module(body=[factory], type_ignores=[]), '<factory>', 'exec'), namespace)
        app = namespace['build_ptb_application']()
        self.assertEqual(app.bot.base_url, 'https://worker.example/telegram-bot/bot123:test')
        self.assertEqual(app.bot.base_file_url, 'https://worker.example/telegram-bot/file/bot123:test')
        self.assertEqual(app.bot.request._client.headers['X-Proxy-Token'], 'test-proxy-token')
