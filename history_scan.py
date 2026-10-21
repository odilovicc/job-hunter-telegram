"""On-demand channel history scanning, with one bounded background scan."""
import asyncio
import logging
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

log = logging.getLogger("vacancy_bot")
SCAN_BUTTON = "🔎 Скан истории"


class MessageGate:
    """Serialize the same message across live updates and history scans."""
    def __init__(self):
        self.entries = {}

    async def run(self, key, action):
        entry = self.entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                return await action()
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self.entries[key]


class HistoryScanner:
    def __init__(self, client, channels, process, admin_id, max_days=365, conn=None):
        self.conn = conn or sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE IF NOT EXISTS scan_results (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(scan_results)")}
        if "run_id" not in columns:
            self.conn.execute("ALTER TABLE scan_results ADD COLUMN run_id INTEGER NOT NULL DEFAULT 0")
        self.conn.execute("CREATE TABLE IF NOT EXISTS scan_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL)")
        self.conn.commit()
        self.run_id = 0
        self.client = client
        self.channels = list(dict.fromkeys(channels))
        self.process = process
        self.admin_id = admin_id
        self.max_days = max_days
        self.task = None
        self.stop_requested = False
        self.counts = Counter()
        self.channel = ""
        self.error_channels = []
        self.start_time = None
        self.end_time = None
        self.completed = 0
        self.last_progress = 0

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def allowed(self, update):
        return (update.effective_user and update.effective_user.id == self.admin_id
                and update.effective_chat and update.effective_chat.id == self.admin_id)

    def register(self, app):
        # Register before HH's catch-all text handler and legacy callbacks.
        app.add_handler(CommandHandler("scan", self.menu))
        app.add_handler(CommandHandler("scan_results", self.results))
        app.add_handler(CommandHandler("scan_status", self.status))
        app.add_handler(CommandHandler("scan_stop", self.stop))
        app.add_handler(MessageHandler(filters.Regex("^" + SCAN_BUTTON + "$"), self.menu))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^scan:"))

    def report(self, heading):
        period = ""
        if self.start_time:
            period = (f"\nПериод UTC: {self.start_time:%d.%m.%Y %H:%M} — "
                      f"{self.end_time:%d.%m.%Y %H:%M}")
        return (heading + period + f"\nКаналов завершено: {self.completed}/{len(self.channels)}"
                + (f"\nТекущий канал: {self.channel}" if self.running and self.channel else "")
                + f"\nПросмотрено сообщений: {self.counts['read']}"
                + f"\nНовых подходящих вакансий: {self.counts['delivered']}"
                + f"\nУже обработано: {self.counts['duplicate']}"
                + f"\nОтсеяно: {self.counts['rejected']}"
                + f"\nБез текста: {self.counts['empty']}"
                + f"\nОшибок сообщений: {self.counts['failed']}"
                + f"\nОшибок каналов: {len(self.error_channels)}"
                + ("\nНедоступны: " + ", ".join(self.error_channels)[:500] if self.error_channels else ""))

    async def menu(self, update, context):
        if not self.allowed(update):
            return
        context.user_data.pop("hh_waiting", None)
        if context.args:
            try:
                if len(context.args) != 1:
                    raise ValueError()
                days = int(context.args[0])
                if not 1 <= days <= self.max_days:
                    raise ValueError()
            except ValueError:
                await update.effective_message.reply_text(f"Укажите число дней: /scan 5 (от 1 до {self.max_days}).")
                return
            return await self.start(update.effective_message, context, days)
        days = [d for d in (1, 2, 3, 4, 5, 6, 7, 14, 30) if d <= self.max_days]
        buttons = [InlineKeyboardButton(f"{d} дн.", callback_data=f"scan:days:{d}") for d in days]
        rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
        rows.append([InlineKeyboardButton("Статус", callback_data="scan:status"),
                     InlineKeyboardButton("Остановить", callback_data="scan:stop")])
        rows.append([InlineKeyboardButton("Сохранённые подборки", callback_data="scan:results")])
        await update.effective_message.reply_text(
            "За сколько дней проверить все настроенные каналы?\n"
            "Период отсчитывается назад от момента запуска: 3 дня = последние 72 часа.\n"
            f"Другой период: /scan 10 (до {self.max_days} дней). Уже обработанные вакансии пропускаются.",
            reply_markup=InlineKeyboardMarkup(rows))

    async def start(self, message, context, days, since=None):
        if self.running:
            await message.reply_text("Сканирование уже идёт. /scan_status — прогресс, /scan_stop — остановить.")
            return
        self.stop_requested = False
        self.counts = Counter()
        self.error_channels = []
        self.channel = ""
        self.completed = 0
        self.end_time = datetime.now(timezone.utc)
        self.start_time = since or self.end_time - timedelta(days=days)
        self.last_progress = time.monotonic()
        self.run_id = self.conn.execute("INSERT INTO scan_runs(started_at) VALUES (?)",
                                        (self.end_time.isoformat(),)).lastrowid
        self.conn.commit()
        # Reserve before the first await, including concurrent startup/manual requests.
        self.task = context.application.create_task(self.run(message), update=None)
        await message.reply_text("Сканирование запущено. Подходящие вакансии соберу в подборку и покажу после завершения.\n"
                                 "/scan_status — прогресс, /scan_stop — остановить.")

    async def status(self, update, context):
        if self.allowed(update):
            await update.effective_message.reply_text(self.report("Сканирование идёт" if self.running else "Сканирование не запущено / завершено"))

    async def stop(self, update, context):
        if self.allowed(update):
            self.stop_requested = True
            await update.effective_message.reply_text(
                "Остановлю после текущего сообщения или запроса истории." if self.running else "Активного сканирования нет.")

    async def callback(self, update, context):
        if not self.allowed(update):
            await update.callback_query.answer("Нет доступа.", show_alert=True)
            return
        await update.callback_query.answer()
        parts = update.callback_query.data.split(":")
        if len(parts) == 3 and parts[1] == "view" and parts[2].isdigit():
            return await self.show(update.effective_message, int(parts[2]))
        if parts == ["scan", "results"]:
            return await self.results(update, context)
        if parts == ["scan", "status"]:
            return await self.status(update, context)
        if parts == ["scan", "stop"]:
            return await self.stop(update, context)
        if len(parts) == 3 and parts[:2] == ["scan", "days"] and parts[2].isdigit():
            days = int(parts[2])
            if 1 <= days <= self.max_days:
                context.user_data.pop("hh_waiting", None)
                await self.start(update.effective_message, context, days)

    async def send_message(self, **payload):
        # The normal processing path marks a vacancy processed only after this
        # durable write. A restart cannot discard a collected vacancy.
        markup = payload.get("reply_markup")
        if markup:
            payload["reply_markup"] = markup.to_dict()
        self.conn.execute("INSERT INTO scan_results(payload, run_id) VALUES (?, ?)",
                          (json.dumps(payload), self.run_id))
        self.conn.commit()

    async def results(self, update, context):
        if self.allowed(update):
            rows = self.conn.execute(
                "SELECT r.run_id, MIN(r.id), COUNT(*), s.started_at FROM scan_results r "
                "LEFT JOIN scan_runs s ON s.id=r.run_id GROUP BY r.run_id ORDER BY r.run_id DESC LIMIT 10"
            ).fetchall()
            if not rows:
                await update.effective_message.reply_text("Сохранённых вакансий пока нет. Запустите /scan.")
                return
            buttons = [[InlineKeyboardButton(
                f"{datetime.fromisoformat(started):%d.%m %H:%M} UTC · {count} вакансий" if started else f"Подборка · {count} вакансий",
                callback_data=f"scan:view:{first}")] for _, first, count, started in rows]
            await update.effective_message.reply_text("Выберите подборку:", reply_markup=InlineKeyboardMarkup(buttons))

    async def show(self, message, identifier):
        row = self.conn.execute("SELECT id, payload, run_id FROM scan_results WHERE id = ?",
                                (identifier,)).fetchone()
        if not row:
            await message.reply_text("Все сохранённые вакансии просмотрены. /scan_results — открыть подборку заново.")
            return
        key, raw, run_id = row
        if self.running and run_id == self.run_id:
            await message.reply_text("Подборка готовится. Дождитесь завершения сканирования.")
            return
        payload = json.loads(raw)
        rows = []
        # Review keeps the existing per-vacancy actions; sending still requires
        # the owner's explicit click, never happens during scanning.
        for buttons in payload.get("reply_markup", {}).get("inline_keyboard", []):
            rows.append([InlineKeyboardButton(**button) for button in buttons])
        next_row = self.conn.execute("SELECT MIN(id) FROM scan_results WHERE run_id=? AND id>?", (run_id, key)).fetchone()
        if next_row[0] is not None:
            rows.append([InlineKeyboardButton("Следующая вакансия →", callback_data=f"scan:view:{next_row[0]}")])
        else:
            rows.append([InlineKeyboardButton("Все вакансии подборки просмотрены · К подборкам", callback_data="scan:results")])
        payload["reply_markup"] = InlineKeyboardMarkup(rows)
        await message.get_bot().send_message(**payload)

    async def notify(self, message, text, **kwargs):
        try:
            await message.reply_text(text, **kwargs)
        except Exception:
            log.warning("Не удалось доставить прогресс сканирования", exc_info=True)

    async def run(self, message):
        try:
            for channel in self.channels:
                if self.stop_requested:
                    break
                self.channel = str(channel)
                try:
                    # Descending order lets us stop at the lower bound; upper bound is
                    # frozen so live updates arriving during a long scan aren't chased.
                    async for item in self.client.iter_messages(channel, offset_date=self.end_time):
                        if self.stop_requested:
                            break
                        if item.date < self.start_time:
                            break
                        if item.date > self.end_time:
                            continue
                        self.counts['read'] += 1
                        try:
                            outcome = await self.process(item, self)
                            self.counts[outcome or 'empty'] += 1
                        except Exception:
                            self.counts['failed'] += 1
                            log.exception("Ошибка обработки истории: channel=%s id=%s", channel, item.id)
                        if time.monotonic() - self.last_progress >= 60:
                            self.last_progress = time.monotonic()
                            await self.notify(message, self.report("Сканирование идёт"))
                        await asyncio.sleep(0.1)
                except Exception:
                    self.error_channels.append(str(channel))
                    log.warning("Не удалось просканировать канал %s", channel, exc_info=True)
                if not self.stop_requested:
                    self.completed += 1
            self.channel = ""
            first = self.conn.execute("SELECT MIN(id) FROM scan_results WHERE run_id=?", (self.run_id,)).fetchone()[0]
            heading = "Сканирование остановлено" if self.stop_requested else "Сканирование завершено"
            if first is not None:
                heading += ", вот ваши подходящие вакансии"
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Посмотреть вакансии", callback_data=f"scan:view:{first}")]])
            else:
                heading += ". Новых подходящих вакансий нет."
                markup = None
            await self.notify(message, self.report(heading) + "\n/scan_results — сохранённые подборки.", reply_markup=markup)
        except asyncio.CancelledError:
            raise

    async def shutdown(self):
        # Cooperative stop preserves the delivery/mark_processed boundary.
        self.stop_requested = True
        if self.running:
            await self.task
