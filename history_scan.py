"""On-demand channel history scanning, with one bounded background scan."""
import asyncio
import logging
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
    def __init__(self, client, channels, process, admin_id, max_days=365):
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
        # Reserve before the first await, including concurrent startup/manual requests.
        self.task = context.application.create_task(self.run(message), update=None)
        await message.reply_text("Сканирование запущено. /scan_status — прогресс, /scan_stop — остановить.")

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
        if parts == ["scan", "status"]:
            return await self.status(update, context)
        if parts == ["scan", "stop"]:
            return await self.stop(update, context)
        if len(parts) == 3 and parts[:2] == ["scan", "days"] and parts[2].isdigit():
            days = int(parts[2])
            if 1 <= days <= self.max_days:
                context.user_data.pop("hh_waiting", None)
                await self.start(update.effective_message, context, days)

    async def notify(self, message, text):
        try:
            await message.reply_text(text)
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
                            outcome = await self.process(item, message.get_bot())
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
            await self.notify(message, self.report("Сканирование остановлено" if self.stop_requested else "Сканирование завершено"))
        except asyncio.CancelledError:
            raise

    async def shutdown(self):
        # Cooperative stop preserves the delivery/mark_processed boundary.
        self.stop_requested = True
        if self.running:
            await self.task
