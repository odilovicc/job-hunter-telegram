import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hh_handlers import HHHandlers
from resume_storage import resume_path


class ResumeUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.target = self.base / 'uploads' / 'resume.pdf'
        self.service = SimpleNamespace(resume_path=str(self.base / 'missing.pdf'),
                                       profile=AsyncMock(side_effect=FileNotFoundError))
        self.saved = Mock()
        self.handler = HHHandlers(self.service, 123, self.target, self.saved)
        async def download(custom_path):
            Path(custom_path).write_bytes(b'new resume')
        self.remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
        self.document = SimpleNamespace(file_name='../../resume.PDF', file_size=100,
                                       get_file=AsyncMock(return_value=self.remote))
        self.message = SimpleNamespace(document=self.document, reply_text=AsyncMock())
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                                      effective_chat=SimpleNamespace(id=123), effective_message=self.message)
        self.context = SimpleNamespace(user_data={'hh_waiting': True, 'hh_profile_hash': 'old'})

    async def test_upload_updates_both_consumers_and_survives_restart(self):
        with patch('hh_handlers.read_resume', return_value={'facts': ['Developer']}):
            await self.handler.upload_resume(self.update, self.context)
        self.assertEqual(self.target.read_bytes(), b'new resume')
        self.assertEqual(self.service.resume_path, str(self.target))
        self.saved.assert_called_once_with(self.target)
        self.assertEqual(resume_path(self.base, {'user_profile': {'resume_path': '/missing'}}), str(self.target))
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        self.assertNotIn('hh_profile_hash', self.context.user_data)

    async def test_invalid_pdf_preserves_previous_file(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'old resume')
        with patch('hh_handlers.read_resume', side_effect=ValueError('Нужен текстовый PDF')):
            await self.handler.upload_resume(self.update, self.context)
        self.assertEqual(self.target.read_bytes(), b'old resume')
        self.saved.assert_not_called()
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    async def test_unauthorized_upload_is_ignored(self):
        self.update.effective_user.id = 42
        await self.handler.upload_resume(self.update, self.context)
        self.document.get_file.assert_not_awaited()

    async def test_oversized_file_is_not_downloaded(self):
        self.document.file_size = 11 * 1024 * 1024
        await self.handler.upload_resume(self.update, self.context)
        self.document.get_file.assert_not_awaited()

    async def test_download_failure_preserves_resume(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'old')
        self.remote.download_to_drive.side_effect = OSError('failed')
        await self.handler.upload_resume(self.update, self.context)
        self.assertEqual(self.target.read_bytes(), b'old')
        self.saved.assert_not_called()
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    async def test_missing_profile_offers_config_and_upload(self):
        await self.handler.profile(self.update, self.context)
        text = self.message.reply_text.call_args.args[0]
        self.assertIn('user_profile.resume_path', text)
        self.assertIn('пришлите', text)
        self.assertIn(self.service.resume_path, text)
        self.assertNotIn('hh_waiting', self.context.user_data)

    def test_missing_empty_relative_and_home_paths(self):
        for config in ({}, {'user_profile': None}, {'user_profile': {'resume_path': ''}}):
            self.assertEqual(resume_path(self.base, config), str(self.base / 'resume.pdf'))
        self.assertEqual(resume_path(self.base, {'user_profile': {'resume_path': 'cv.pdf'}}), str(self.base / 'cv.pdf'))
        self.assertEqual(resume_path(self.base, {'user_profile': {'resume_path': '~/cv.pdf'}}), str(Path.home() / 'cv.pdf'))
