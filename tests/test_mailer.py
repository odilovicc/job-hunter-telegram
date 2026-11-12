import json
import smtplib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hh_handlers import HHHandlers
from mailer import GmailSender, MailError, Outbox, find_emails, safe_filename, valid_email

CONFIG = {'gmail_address': 'me@gmail.com', 'app_password': 'abcd efgh ijkl mnop', 'from_name': 'Ivan Petrov',
          'signature': '--\nIvan'}


class MailerUnitTests(unittest.TestCase):
    def test_find_emails_dedupes_and_skips_service_addresses(self):
        text = 'Резюме: HR@Corp.uz. Копия hr@corp.uz, noreply@corp.uz, me@gmail.com и jobs@corp.co.uk.'
        self.assertEqual(find_emails(text, exclude=['me@gmail.com']), ['HR@Corp.uz', 'jobs@corp.co.uk'])

    def test_valid_email(self):
        self.assertTrue(valid_email('a.b+tag@mail.example.uz'))
        for bad in ('a@b', 'a@@b.com', 'a b@c.com', '', 'x@y.c'):
            self.assertFalse(valid_email(bad), bad)

    def test_subject_body_and_message(self):
        sender = GmailSender(CONFIG)
        self.assertEqual(sender.password, 'abcdefghijklmnop')
        self.assertEqual(sender.resume_filename, 'CV_Ivan_Petrov.pdf')
        self.assertEqual(sender.subject({'name': 'Vue Developer', 'company': 'Acme'}),
                         'Отклик на вакансию «Vue Developer» — Ivan Petrov')
        self.assertEqual(sender.subject({'name': ''}), 'Отклик на вакансию — Ivan Petrov')
        with tempfile.NamedTemporaryFile(suffix='.pdf') as pdf:
            pdf.write(b'%PDF-1.4 test'); pdf.flush()
            msg = sender.build('hr@corp.uz', 'Тема', sender.body('Здравствуйте!'), pdf.name)
        self.assertEqual(msg['From'], 'Ivan Petrov <me@gmail.com>')
        self.assertTrue(msg['Message-ID'].endswith('@gmail.com>'))
        attachment = next(msg.iter_attachments())
        self.assertEqual(attachment.get_filename(), 'CV_Ivan_Petrov.pdf')
        self.assertEqual(attachment.get_content(), b'%PDF-1.4 test')
        self.assertEqual(msg.get_body().get_content(), 'Здравствуйте!\n\n--\nIvan\n')

    def test_env_overrides_and_filename_sanitized(self):
        with patch.dict('os.environ', {'GMAIL_APP_PASSWORD': 'env-secret'}):
            self.assertEqual(GmailSender(CONFIG).password, 'env-secret')
        self.assertEqual(safe_filename('../../Резюме Иван'), 'Резюме_Иван.pdf')
        self.assertFalse(GmailSender({}).configured)

    def test_auth_error_explains_app_password(self):
        sender = GmailSender(CONFIG)
        with patch('mailer.smtplib.SMTP_SSL') as cls:
            cls.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'bad')
            with self.assertRaisesRegex(MailError, 'пароль приложения'):
                sender._send_sync({'Message-ID': '<x>'})


class OutboxTests(unittest.TestCase):
    def test_claim_is_exclusive_and_restart_releases_sending(self):
        conn = sqlite3.connect(':memory:')
        box = Outbox(conn)
        draft = box.draft(1, 'hr@corp.uz', 'S', 'B')
        self.assertTrue(box.claim(draft))
        self.assertFalse(box.claim(draft))
        Outbox(conn)  # рестарт посреди отправки возвращает черновик
        self.assertTrue(box.claim(draft))
        box.mark_sent(draft, '<id>')
        self.assertFalse(box.claim(draft))
        self.assertIsNotNone(box.last_sent_to('HR@corp.uz'))


class MailHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.resume = Path(tmp.name) / 'resume.pdf'
        self.resume.write_bytes(b'%PDF')
        vacancy = {'id': '1', 'name': 'Vue Dev', 'company': 'Acme', 'skills': [],
                   'description': 'Присылайте резюме на hr@acme.uz или jobs@acme.uz'}
        job = {'id': 7, 'status': 'done', 'letter': 'Здравствуйте!', 'vacancy': json.dumps(vacancy)}
        self.service = SimpleNamespace(resume_path=str(self.resume), get=lambda i: job if i == 7 else None)
        self.mailer = GmailSender(CONFIG)
        self.mailer.send = AsyncMock(return_value='<mid@gmail.com>')
        self.outbox = Outbox(sqlite3.connect(':memory:'))
        self.handler = HHHandlers(self.service, 123, self.resume, mailer=self.mailer, outbox=self.outbox)
        self.context = SimpleNamespace(user_data={})

    def update(self, data=None, text=None):
        message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_document=AsyncMock())
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                                message=message) if data else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123),
                               effective_message=message, callback_query=query)

    async def test_detected_email_preview_then_single_send(self):
        update = self.update('hh:mail:7')
        await self.handler.callback(update, self.context)
        preview, kwargs = update.effective_message.reply_text.call_args
        self.assertIn('hr@acme.uz', preview[0])
        self.assertIn('jobs@acme.uz', preview[0])  # альтернативный адрес показан
        self.assertIn('Vue Dev', preview[0])
        send_data = kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        self.mailer.send.assert_not_called()  # без подтверждения не отправляем

        for _ in range(2):
            await self.handler.callback(self.update(send_data), self.context)
        self.mailer.send.assert_awaited_once()
        to, subject, body, attachment = self.mailer.send.call_args.args
        self.assertEqual((to, attachment), ('hr@acme.uz', str(self.resume)))
        self.assertTrue(body.startswith('Здравствуйте!'))

    async def test_manual_address_when_none_found_and_failure_allows_retry(self):
        job = self.service.get(7)
        job['vacancy'] = json.dumps(dict(json.loads(job['vacancy']), description='Пишите в Telegram'))
        await self.handler.callback(self.update('hh:mail:7'), self.context)
        self.assertEqual(self.context.user_data['mail_wait'], 7)
        bad = self.update(text='not@valid')
        await self.handler.text(bad, self.context)
        self.assertIn('Не похоже', bad.effective_message.reply_text.call_args.args[0])
        good = self.update(text='mailto:boss@acme.uz')
        await self.handler.text(good, self.context)
        self.assertNotIn('mail_wait', self.context.user_data)
        send_data = good.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data

        self.mailer.send.side_effect = MailError('Сеть недоступна', retryable=True)
        failed = self.update(send_data)
        await self.handler.callback(failed, self.context)
        self.assertIn('Сеть недоступна', failed.callback_query.edit_message_text.call_args.args[0])
        self.mailer.send.side_effect = None
        await self.handler.callback(self.update(send_data), self.context)
        self.assertEqual(self.mailer.send.await_count, 2)

    async def test_unconfigured_mail_explains_setup(self):
        self.handler.mailer = GmailSender({})
        update = self.update('hh:mail:7')
        await self.handler.callback(update, self.context)
        self.assertIn('apppasswords', update.effective_message.reply_text.call_args.args[0])

    async def test_resume_button_uploads_once_then_reuses_file_id(self):
        first = self.update(text='📎 Моё резюме')
        first.effective_message.reply_document.return_value = SimpleNamespace(document=SimpleNamespace(file_id='FID'))
        await self.handler.text(first, self.context)
        self.assertEqual(first.effective_message.reply_document.call_args.kwargs['filename'], 'CV_Ivan_Petrov.pdf')
        second = self.update(text='📎 Моё резюме')
        await self.handler.text(second, self.context)
        self.assertEqual(second.effective_message.reply_document.call_args.args[0], 'FID')
        self.resume.write_bytes(b'%PDF new version')
        third = self.update(text='📎 Моё резюме')
        await self.handler.text(third, self.context)
        self.assertNotEqual(third.effective_message.reply_document.call_args.args[0], 'FID')


if __name__ == '__main__':
    unittest.main()
