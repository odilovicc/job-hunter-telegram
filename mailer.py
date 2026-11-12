"""Отправка сопроводительных писем через Gmail SMTP.

Почему SMTP + пароль приложения, а не Gmail API: для личного бота это одна
строка в конфиге и ноль OAuth-обвязки (consent screen, refresh-токены, их
протухание). Gmail сам кладёт письмо в «Отправленные», ответы HR приходят в
обычный ящик. Пароль приложения создаётся на myaccount.google.com/apppasswords
(нужна двухэтапная аутентификация) и отзывается там же, не трогая основной пароль.
"""
import asyncio
import logging
import mimetypes
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

log = logging.getLogger("vacancy_bot")

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
# Достаточно строгий для опечаток, без попытки реализовать RFC 5322 целиком.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
# Адреса, на которые отклик слать бессмысленно.
SKIP_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|mailer-daemon|postmaster|abuse|privacy|support)$", re.I)


def valid_email(value):
    return bool(EMAIL_RE.fullmatch(value or "")) and len(value) <= 254


def find_emails(text, exclude=()):
    """Адреса из текста вакансии в порядке появления, без дублей и служебных."""
    skip = {e.lower() for e in exclude if e}
    seen, result = set(), []
    for match in EMAIL_RE.findall(text or ""):
        email = match.rstrip(".")
        key = email.lower()
        if key in seen or key in skip or SKIP_LOCAL.match(key.split("@")[0]):
            continue
        seen.add(key)
        result.append(email)
    return result


def safe_filename(name, fallback="CV.pdf"):
    name = re.sub(r"[^\w.\- ]+", "", str(name or ""), flags=re.UNICODE).strip(" .").replace(" ", "_")
    if not name:
        return fallback
    return name if name.lower().endswith(".pdf") else name + ".pdf"


class MailError(Exception):
    """Ошибка с понятным пользователю текстом; retryable — есть смысл повторить."""
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class GmailSender:
    def __init__(self, config):
        config = config or {}
        self.address = (os.environ.get("GMAIL_ADDRESS") or config.get("gmail_address") or "").strip()
        # Переменная окружения приоритетнее: секрет не обязан лежать в config.yaml.
        # Google показывает пароль приложения с пробелами — они не нужны.
        self.password = (os.environ.get("GMAIL_APP_PASSWORD") or config.get("app_password") or "").replace(" ", "")
        self.from_name = (config.get("from_name") or "").strip()
        self.subject_template = config.get("subject_template") or "Отклик на вакансию «{vacancy}» — {name}"
        self.subject_fallback = config.get("subject_fallback") or "Отклик на вакансию — {name}"
        self.signature = (config.get("signature") or "").strip()
        self.resume_filename = safe_filename(
            config.get("resume_filename") or (f"CV_{self.from_name}" if self.from_name else ""))
        self.timeout = int(config.get("timeout_sec", 30))

    @property
    def configured(self):
        return bool(self.address and self.password)

    def subject(self, vacancy):
        name = self.from_name or self.address
        title, company = (vacancy.get("name") or "").strip(), (vacancy.get("company") or "").strip()
        if not title:
            return self.subject_fallback.format(name=name, company=company)
        return self.subject_template.format(vacancy=title, company=company, name=name)

    def body(self, letter):
        return letter.strip() + ("\n\n" + self.signature if self.signature else "") + "\n"

    def build(self, to, subject, body, attachment):
        msg = EmailMessage()
        msg["From"] = formataddr((self.from_name, self.address)) if self.from_name else self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        # Свой Message-ID с доменом отправителя: по нему видно письмо в логах
        # и Gmail не подставляет случайный.
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1] or None)
        msg.set_content(body)
        data = Path(attachment).read_bytes()
        maintype, subtype = (mimetypes.guess_type(self.resume_filename)[0] or "application/pdf").split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=self.resume_filename)
        return msg

    def _send_sync(self, msg):
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=self.timeout, context=context) as smtp:
                smtp.login(self.address, self.password)
                refused = smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError("Gmail отклонил вход. Нужен пароль приложения (не обычный пароль): "
                            "myaccount.google.com/apppasswords, двухэтапная аутентификация должна быть включена.") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError("Gmail отклонил адрес получателя. Проверьте email.") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise MailError(f"Gmail не принял письмо ({exc.smtp_code}). Возможно, превышен дневной лимит "
                            "или вложение слишком большое.", retryable=True) from exc
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as exc:
            raise MailError("Не удалось подключиться к smtp.gmail.com:465. Проверьте сеть сервера "
                            "(некоторые хостинги блокируют SMTP).", retryable=True) from exc
        except smtplib.SMTPException as exc:
            raise MailError(f"Ошибка SMTP: {type(exc).__name__}.", retryable=True) from exc
        if refused:
            raise MailError("Gmail отклонил адрес получателя. Проверьте email.")
        return msg["Message-ID"]

    async def send(self, to, subject, body, attachment):
        if not self.configured:
            raise MailError("Почта не настроена: заполните email.gmail_address и email.app_password в config.yaml.")
        if not valid_email(to):
            raise MailError("Некорректный email получателя.")
        msg = await asyncio.to_thread(self.build, to, subject, body, attachment)
        return await asyncio.to_thread(self._send_sync, msg)


class Outbox:
    """Черновики и журнал писем: кнопки переживают рестарт, двойной клик не шлёт дважды."""

    def __init__(self, conn):
        self.conn = conn
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS email_outbox (
                id INTEGER PRIMARY KEY, letter_id INTEGER, recipient TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                message_id TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, sent_at TEXT);
            CREATE INDEX IF NOT EXISTS email_outbox_recipient ON email_outbox(recipient, status);
            UPDATE email_outbox SET status='draft' WHERE status='sending';
        ''')
        conn.commit()

    def draft(self, letter_id, recipient, subject, body):
        cur = self.conn.execute(
            "INSERT INTO email_outbox(letter_id, recipient, subject, body) VALUES (?,?,?,?)",
            (letter_id, recipient, subject, body))
        self.conn.commit()
        return cur.lastrowid

    def get(self, draft_id):
        cur = self.conn.execute("SELECT * FROM email_outbox WHERE id=?", (draft_id,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def claim(self, draft_id):
        """Ровно один обработчик переводит черновик в 'sending'."""
        with self.conn:
            cur = self.conn.execute("UPDATE email_outbox SET status='sending' WHERE id=? AND status='draft'", (draft_id,))
        return cur.rowcount == 1

    def mark_sent(self, draft_id, message_id):
        with self.conn:
            self.conn.execute("UPDATE email_outbox SET status='sent', message_id=?, error=NULL, "
                              "sent_at=CURRENT_TIMESTAMP WHERE id=?", (message_id, draft_id))

    def release(self, draft_id, error):
        with self.conn:
            self.conn.execute("UPDATE email_outbox SET status='draft', error=? WHERE id=?", (error, draft_id))

    def cancel(self, draft_id):
        with self.conn:
            self.conn.execute("UPDATE email_outbox SET status='cancelled' WHERE id=? AND status='draft'", (draft_id,))

    def last_sent_to(self, recipient):
        row = self.conn.execute("SELECT sent_at FROM email_outbox WHERE recipient=? COLLATE NOCASE "
                                "AND status='sent' ORDER BY sent_at DESC LIMIT 1", (recipient,)).fetchone()
        return row[0] if row else None
