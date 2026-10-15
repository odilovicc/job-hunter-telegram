"""Private Telegram conversation for manual HH letters."""
import asyncio
import json
import logging
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from hh_letters import fetch_vacancy, vacancy_id, read_resume

log = logging.getLogger("vacancy_bot")
MENU = ReplyKeyboardMarkup([["✉️ Письмо для HH", "📄 Мой профиль"], ["🔎 Скан истории"]], resize_keyboard=True)


class HHHandlers:
    def __init__(self, service, admin_id, upload_path=None, on_resume_saved=None):
        self.service, self.admin_id = service, admin_id
        self.active = set()
        self.upload_path = Path(upload_path or service.resume_path)
        self.on_resume_saved = on_resume_saved

    def allowed(self, update):
        return (update.effective_user and update.effective_user.id == self.admin_id
                and update.effective_chat and update.effective_chat.id == self.admin_id)

    def register(self, app):
        app.add_handler(CommandHandler("letter", self.start))
        app.add_handler(CommandHandler("resume", self.profile))
        app.add_handler(CommandHandler("letters", self.jobs))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^hh:"))
        app.add_handler(MessageHandler(filters.Document.ALL, self.upload_resume))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))

    async def start(self, update, context):
        if not self.allowed(update):
            return
        context.user_data["hh_waiting"] = True
        await update.effective_message.reply_text(
            "Пришлите ссылку https://hh.uz/vacancy/… или текст вакансии.\n"
            "Первый раз проверьте резюме через /resume. Отмена ввода: /cancel.", reply_markup=MENU)

    async def cancel(self, update, context):
        if self.allowed(update):
            context.user_data.pop("hh_waiting", None)
            await update.effective_message.reply_text("Ввод вакансии отменён. Уже поставленные задачи остаются в очереди.")

    async def profile(self, update, context):
        if not self.allowed(update):
            return
        context.user_data.pop("hh_waiting", None)
        context.user_data.pop("hh_profile_hash", None)
        try:
            profile = await self.service.profile()
        except Exception as exc:
            log.warning("Resume read failed (%s)", type(exc).__name__)
            reason = str(exc) if isinstance(exc, ValueError) else "Файл отсутствует, недоступен или не является читаемым PDF."
            await update.effective_message.reply_text(
                f"Не удалось прочитать резюме. {reason}\n"
                f"Текущий путь: {self.service.resume_path}\n"
                "Укажите user_profile.resume_path в config.yaml и перезапустите бота. "
                "Относительный путь считается от папки проекта; файл должен быть на машине бота.\n"
                "Или пришлите сюда резюме файлом: текстовый PDF до 10 МБ и 30 страниц. "
                "Я скачаю его и обновлю профиль автоматически.")
            return
        await update.effective_message.reply_text(
            "Факты, извлечённые из PDF локально. Проверьте текст: только эти данные будут основой письма. "
            "Чтобы обновить резюме, пришлите сюда новый PDF файлом (до 10 МБ, 30 страниц).")
        text = "\n".join(profile["facts"])
        for offset in range(0, len(text), 3500):
            await update.effective_message.reply_text(text[offset:offset + 3500])
        context.user_data["hh_profile_hash"] = profile["hash"]
        await update.effective_message.reply_text(
            "Профиль подтверждён." if profile["approved"] else "Данные извлечены верно?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Подтвердить профиль", callback_data="hh:approve")]]))

    async def upload_resume(self, update, context):
        if not self.allowed(update):
            return
        document = update.effective_message.document
        if not (document.file_name or "").lower().endswith(".pdf"):
            await update.effective_message.reply_text("Пришлите резюме файлом в формате PDF.")
            return
        if document.file_size and document.file_size > 10 * 1024 * 1024:
            await update.effective_message.reply_text("Резюме больше 10 МБ. Пришлите PDF меньшего размера.")
            return
        temporary = None
        try:
            self.upload_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.upload_path.parent, suffix=".pdf", delete=False) as file:
                temporary = Path(file.name)
            remote = await document.get_file()
            await remote.download_to_drive(custom_path=temporary)
            await asyncio.to_thread(read_resume, temporary)
            temporary.replace(self.upload_path)
            self.service.resume_path = str(self.upload_path)
            if self.on_resume_saved:
                self.on_resume_saved(self.upload_path)
        except Exception as exc:
            log.warning("Resume upload failed (%s)", type(exc).__name__)
            reason = str(exc) if isinstance(exc, ValueError) else "Не удалось скачать, прочитать или сохранить PDF. Попробуйте ещё раз."
            await update.effective_message.reply_text(reason + " Предыдущее резюме не заменено.")
            return
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        await update.effective_message.reply_text("Резюме сохранено и будет использоваться для новых откликов, в том числе после перезапуска.")
        await self.profile(update, context)

    async def jobs(self, update, context):
        if not self.allowed(update):
            return
        rows = self.service.failed_jobs()
        await update.effective_message.reply_text(
            "Незавершённые письма (последние 10). Повтор использует сохранённые данные." if rows else "Незавершённых писем нет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Повторить письмо №{row[0]}", callback_data=f"hh:retry:{row[0]}")] for row in rows]) if rows else None)

    async def deliver(self, message, identifier):
        if identifier in self.active:
            await message.reply_text("Это письмо уже в очереди.")
            return
        self.active.add(identifier)
        try:
            letter, cached = await self.service.generate(identifier)
            job = self.service.get(identifier)
            vacancy = json.loads(job["vacancy"])
            buttons = []
            if vacancy["id"].isdigit():
                buttons.append([InlineKeyboardButton("Открыть вакансию", url=f'https://hh.uz/vacancy/{vacancy["id"]}')])
            buttons.append([InlineKeyboardButton("Сделать короче", callback_data=f"hh:short:{identifier}"),
                            InlineKeyboardButton("Переписать", callback_data=f"hh:rewrite:{identifier}")])
            await message.reply_text("Готово · из кеша" if cached else "Письмо готово", reply_markup=InlineKeyboardMarkup(buttons))
            await message.reply_text(letter)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("HH letter %s failed (%s)", identifier, type(exc).__name__)
            reason = ""
            if getattr(exc, "code", None) == 429:
                reason = "Gemini ограничил запросы или исчерпана квота. "
            elif isinstance(exc, asyncio.TimeoutError):
                reason = "Gemini не ответил вовремя. "
            elif isinstance(exc, ValueError):
                reason = str(exc) + " "
            await message.reply_text(
                reason + f"Письмо №{identifier} пока не готово. Задача сохранена; можно повторить через /letters. "
                "При изменении резюме проверьте /resume и пришлите вакансию заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Повторить", callback_data=f"hh:retry:{identifier}")]]))
        finally:
            self.active.discard(identifier)

    async def text(self, update, context):
        if not self.allowed(update):
            return
        text = update.effective_message.text.strip()
        if text == "✉️ Письмо для HH":
            return await self.start(update, context)
        if text == "📄 Мой профиль":
            return await self.profile(update, context)
        if not context.user_data.get("hh_waiting"):
            return
        try:
            if text.startswith(("https://", "http://")):
                identifier = vacancy_id(text)
                vacancy = await fetch_vacancy(identifier, self.service.config.get("hh_user_agent", "PersonalLetterBot/1.0"))
            else:
                if len(text) < 80 or len(text) > 20000:
                    raise ValueError("Пришлите текст вакансии от 80 до 20 000 символов.")
                from hh_letters import digest
                vacancy = {"id": "text-" + digest(text), "name": "", "company": "", "skills": [], "description": text}
            identifier, compacted = await self.service.create(vacancy)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        except Exception as exc:
            log.warning("HH preparation failed (%s)", type(exc).__name__)
            await update.effective_message.reply_text("Не удалось подготовить данные. Проверьте /resume; если HH недоступен, пришлите текст вакансии.")
            return
        await update.effective_message.reply_text(
            f"Письмо №{identifier} в очереди." + (" Длинное описание сокращено по смысловым пунктам." if compacted else ""))
        context.application.create_task(self.deliver(update.effective_message, identifier), update=update)

    async def callback(self, update, context):
        if not self.allowed(update):
            await update.callback_query.answer("Нет доступа.", show_alert=True)
            return
        await update.callback_query.answer()
        parts = update.callback_query.data.split(":")
        action = parts[1]
        message = update.effective_message
        try:
            if action == "approve":
                profile = await self.service.profile()
                if profile["hash"] != context.user_data.get("hh_profile_hash"):
                    raise ValueError("Профиль изменился или бот перезапущен. Проверьте /resume ещё раз.")
                self.service.approve(profile["hash"])
                context.user_data["hh_waiting"] = True
                await message.reply_text("Профиль сохранён. Пришлите ссылку HH или текст вакансии.", reply_markup=MENU)
                return
            if len(parts) != 3 or not parts[2].isdigit():
                return
            identifier = int(parts[2])
            if action in ("short", "rewrite"):
                parent = self.service.get(identifier)
                if not parent or parent["status"] != "done":
                    raise ValueError("Сначала дождитесь готового письма.")
                identifier, _ = await self.service.create(json.loads(parent["vacancy"]), action, parent)
            elif action != "retry":
                return
            await message.reply_text(f"Письмо №{identifier} в очереди.")
            context.application.create_task(self.deliver(message, identifier), update=update)
        except ValueError as exc:
            await message.reply_text(str(exc))
        except Exception as exc:
            log.warning("HH callback failed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось подготовить письмо. Проверьте /resume.")
