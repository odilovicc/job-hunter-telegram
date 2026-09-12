import argparse
import asyncio
import html
import logging
import os
import time
import traceback
import yaml
import qrcode
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.utils import get_peer_id
from google.genai import types as genai_types
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from database import db
from filters import level_1_filter, level_2_scoring
from ai_generator import generate_cover_letter, create_ai_client, FALLBACK_COVER_LETTER

# Пути считаются от расположения скрипта, а не от текущей рабочей директории —
# иначе запуск через systemd/cron с другим WorkingDirectory тихо создаёт
# новую сессию/БД/лог в неожиданном месте.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Telegram режет сообщения на 4096 символах. Если отчёт окажется длиннее,
# send_message упадёт и вакансия не дойдёт до админа — поэтому длину бюджетируем.
TELEGRAM_MSG_LIMIT = 4096

# Минимальный интервал между запросами к Gemini. Раньше это был просто
# `await asyncio.sleep(1)` внутри обработчика, но Telethon запускает обработчики
# NewMessage параллельно, поэтому при пачке сообщений из разных каналов все они
# засыпали одновременно и уходили в API одновременно — никакого rate limit не было.
GEMINI_MIN_INTERVAL_SEC = 1.0
GEMINI_TIMEOUT_SEC = 30

# --- Нормальное логирование вместо print() ---
# RotatingFileHandler — при работе на сервере месяцами bot.log не должен расти бесконечно.
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(os.path.join(BASE_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("vacancy_bot")

# httpx (транспорт python-telegram-bot) на уровне DEBUG логирует каждый
# long-polling запрос — это заливает bot.log мусором раз в несколько секунд.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- Конфиг грузится ОДИН раз ---
with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# --- Валидация конфига на старте ---
# Все проверки делаются до подключения к сети: лучше упасть на первой секунде с
# понятным текстом, чем через час работы обнаружить, что резюме лежит не там.
admin_bot_cfg = config.get("admin_bot") or {}
ADMIN_BOT_TOKEN = (admin_bot_cfg.get("bot_token") or "").strip()
_raw_admin_chat_id = admin_bot_cfg.get("chat_id")

if not ADMIN_BOT_TOKEN:
    raise SystemExit(
        "config.yaml: не задан admin_bot.bot_token. Получите токен у @BotFather "
        "и впишите его (см. config.example.yaml)."
    )

if _raw_admin_chat_id in (None, ""):
    raise SystemExit(
        "config.yaml: не задан admin_bot.chat_id. Узнайте свой числовой id у "
        "@userinfobot и впишите его (см. config.example.yaml)."
    )

try:
    # Приводим к int один раз здесь, а не при каждом сравнении: если оставить
    # строку, проверка «нажал ли кнопку админ» молча сравнивала бы int со строкой
    # и отклоняла вообще все нажатия.
    ADMIN_CHAT_ID = int(str(_raw_admin_chat_id).strip())
except ValueError:
    raise SystemExit(
        f"config.yaml: admin_bot.chat_id должен быть числом, а не {_raw_admin_chat_id!r}."
    )

# resume_path тоже разрешаем относительно BASE_DIR — иначе при запуске из
# systemd/cron с чужим WorkingDirectory отправка падала бы с FileNotFoundError
# уже ПОСЛЕ того, как вакансия помечена обработанной.
_raw_resume_path = config["user_profile"]["resume_path"]
RESUME_PATH = _raw_resume_path if os.path.isabs(_raw_resume_path) else os.path.join(BASE_DIR, _raw_resume_path)

# Не падаем: бот всё равно полезен (фильтрует и показывает вакансии), резюме можно
# положить позже, а неудачная отправка теперь возвращает кнопку для повтора.
# Но предупреждаем громко — и в лог, и админу в стартовом сообщении.
RESUME_MISSING = not os.path.isfile(RESUME_PATH)
if RESUME_MISSING:
    log.error(
        f"Файл резюме не найден: {RESUME_PATH}. Отправка откликов будет падать, "
        f"пока файл не появится (см. user_profile.resume_path)."
    )

# --- Режим сети: "direct" (как раньше) или "cloudflare_worker" ---
# Нужен, когда хостинг бота не имеет нормального прямого доступа к Telegram
# и/или Google (например, сервер в РФ). В режиме cloudflare_worker и MTProto
# (Telethon), и запросы к Gemini идут через Cloudflare Worker — см.
# cloudflare_transport.py и cloudflare-worker/README.md.
network_cfg = config.get("network", {"mode": "direct"})
network_mode = network_cfg.get("mode", "direct")

telethon_kwargs = {}
gemini_http_options = None
# base_url/headers для Bot API (python-telegram-bot) через воркер — None означает
# "обычный прямой api.telegram.org", как раньше.
ptb_base_url = None
ptb_proxy_headers = None

if network_mode == "cloudflare_worker":
    cf_cfg = network_cfg["cloudflare_worker"]
    worker_base = cf_cfg["base_url"].rstrip("/")
    proxy_token = cf_cfg["proxy_token"]

    if worker_base.startswith("https://"):
        ws_base = "wss://" + worker_base[len("https://"):]
    elif worker_base.startswith("http://"):
        ws_base = "ws://" + worker_base[len("http://"):]
    else:
        raise SystemExit(f"network.cloudflare_worker.base_url должен начинаться с http:// или https://: {worker_base!r}")

    from cloudflare_transport import make_cloudflare_connection
    telethon_kwargs["connection"] = make_cloudflare_connection(
        ws_url=f"{ws_base}/mtproto",
        token=proxy_token,
    )
    gemini_http_options = genai_types.HttpOptions(
        base_url=f"{worker_base}/gemini",
        headers={"X-Proxy-Token": proxy_token},
    )
    # Bot API (getMe/sendMessage/getUpdates и т.д.) — это обычный HTTPS с явным
    # SNI api.telegram.org, который многие DPI блокируют отдельно от MTProto.
    # Раньше это не проксировалось вообще, из-за чего Telethon работал, а
    # python-telegram-bot падал с ConnectTimeout/TimedOut на getMe().
    ptb_base_url = f"{worker_base}/telegram-bot/bot"
    ptb_proxy_headers = {"X-Proxy-Token": proxy_token}
    log.info(f"Режим сети: cloudflare_worker ({worker_base})")
else:
    log.info("Режим сети: direct")

# --- Инициализация клиентов ---
# Telethon: юзер-сессия, читает каналы и отправляет резюме от имени пользователя.
# Обычный бот так сделать не может: Bot API не умеет писать первым человеку,
# который не начинал диалог с ботом, а HR почти всегда именно такой.
client = TelegramClient(
    os.path.join(BASE_DIR, "bot_session"),
    config["telegram"]["api_id"],
    config["telegram"]["api_hash"],
    **telethon_kwargs
)
ai_client = create_ai_client(config["ai"]["gemini_api_key"], http_options=gemini_http_options)

# Множество юзернеймов каналов (lower, без @) для фильтрации в extract_contact
channel_usernames = {ch.lstrip("@").lower() for ch in config["telegram"]["channels"]}

# chat_id (marked id вида -100xxxxxxxxxx) -> username канала, для ссылок в /stats.
# Заполняется один раз при старте (см. build_channel_username_map) — не резолвим
# на каждое сообщение, чтобы не дёргать Telethon лишний раз.
channel_username_by_chat_id = {}


def message_link(chat_id, msg_id):
    """Формирует t.me-ссылку на сообщение для отчётов/статистики.

    Если username канала известен — обычная публичная ссылка (открывается у
    кого угодно). Если нет — ссылка вида t.me/c/<id>/<msg>, которая работает
    только у тех, кто уже состоит в этом канале/группе (fallback на случай
    приватных чатов или сбоя резолвинга).
    """
    username = channel_username_by_chat_id.get(chat_id)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    internal_id = s[4:] if s.startswith("-100") else s.lstrip("-")
    return f"https://t.me/c/{internal_id}/{msg_id}"


async def build_channel_username_map():
    """Резолвит username всех каналов из конфига в их chat_id один раз при
    старте — нужно для ссылок на сообщения в /stats и в отчётах."""
    for uname in config["telegram"]["channels"]:
        clean = uname.lstrip("@")
        try:
            entity = await client.get_entity(clean)
            channel_username_by_chat_id[get_peer_id(entity)] = clean
        except Exception as e:
            log.warning(f"Не удалось получить entity канала {clean} для ссылок в /stats: {e}")


# --- Алерты в Telegram при ошибках (Gemini, Bot API, Telethon, необработанные
# исключения) ---
#
# Идея: не расставлять notify_admin() по всем местам вручную, а повесить
# обработчик логов на наш собственный логгер "vacancy_bot" (его используют
# main.py, database.py, ai_generator.py, filters.py). Тогда КАЖДЫЙ существующий
# и будущий log.error()/log.exception() автоматически долетает админу в
# Telegram, а не только в bot.log на сервере, куда для просмотра нужно идти
# через SSH.
_admin_bot_ref: dict = {"bot": None}  # {"bot": Optional[Bot]} — без аннотации типизатор сужает её до dict[str, None]
_pending_alert_tasks = set()


def _make_standalone_bot():
    """Bot, независимый от жизненного цикла ptb_app — нужен, чтобы отправлять
    алерты даже до того, как основной Application проинициализирован (или
    после того, как он уже остановлен, например при фатальном падении)."""
    if ptb_base_url:
        return Bot(
            token=ADMIN_BOT_TOKEN,
            base_url=ptb_base_url,
            request=HTTPXRequest(httpx_kwargs={"headers": ptb_proxy_headers}),
        )
    return Bot(token=ADMIN_BOT_TOKEN)


async def notify_admin(text):
    """Шлёт HTML-текст админу. Любая ошибка при отправке гасится и логируется
    через log.warning (НЕ log.error) — иначе сбой самой отправки алерта мог бы
    зациклить TelegramAlertHandler сам на себя."""
    running_bot = _admin_bot_ref.get("bot")
    try:
        if running_bot is not None:
            await running_bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:4000], parse_mode=ParseMode.HTML)
        else:
            async with _make_standalone_bot() as tmp_bot:
                await tmp_bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:4000], parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning(f"Не удалось отправить алерт админу: {e}")


class TelegramAlertHandler(logging.Handler):
    """Дублирует ERROR/CRITICAL-логи бота админу в Telegram.

    Не блокирует и не роняет логирование: если нет запущенного event loop
    (например, самый ранний старт скрипта) — алерт молча теряется, но в
    bot.log запись всё равно останется благодаря остальным хендлерам.
    """

    DEDUPE_WINDOW_SEC = 300  # не чаще раза в 5 минут на одинаковый (логгер, сообщение)

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._last_sent = {}

    def emit(self, record):
        try:
            key = (record.name, record.getMessage())
            now = time.monotonic()
            last = self._last_sent.get(key)
            if last is not None and now - last < self.DEDUPE_WINDOW_SEC:
                return
            self._last_sent[key] = now

            text = self.format(record)
            loop = asyncio.get_running_loop()
        except Exception:
            return

        icon = "🆘" if record.levelno >= logging.CRITICAL else "⚠️"
        alert_text = f"{icon} <b>Ошибка в боте</b> ({html.escape(record.name)})\n<pre>{html.escape(text[:3500])}</pre>"
        task = loop.create_task(notify_admin(alert_text))
        _pending_alert_tasks.add(task)
        task.add_done_callback(_pending_alert_tasks.discard)


log.addHandler(TelegramAlertHandler())

# Сериализация обращений к Gemini (см. GEMINI_MIN_INTERVAL_SEC).
# Lock создаётся лениво, а не на уровне модуля: до Python 3.10
# asyncio.Lock() привязывается к текущему event loop в момент создания, а на
# импорте модуля нужного loop'а ещё может не быть — тогда первый же await падает
# с "attached to a different loop".
_gemini_lock = None
_gemini_last_call = 0.0


def _get_gemini_lock():
    global _gemini_lock
    if _gemini_lock is None:
        _gemini_lock = asyncio.Lock()
    return _gemini_lock


def extract_contact(text):
    """Ищет юзернейм HR в тексте вакансии. Возвращает None, если не нашёл."""
    usernames = re.findall(r'@([A-Za-z0-9_]{5,32})', text)
    for username in usernames:
        # Пропускаем юзернеймы каналов, чтобы не писать самому каналу
        if username.lower() not in channel_usernames:
            return username
    return None


async def generate_letter(vacancy_text):
    """Генерирует письмо. Возвращает (letter, is_fallback).

    generate_cover_letter делает синхронный сетевой запрос к Gemini — если вызвать
    его напрямую, он блокирует весь event loop (все каналы, все кнопки) на время
    запроса. Уносим в отдельный поток и ограничиваем таймаутом.
    """
    global _gemini_last_call

    async with _get_gemini_lock():
        wait = GEMINI_MIN_INTERVAL_SEC - (time.monotonic() - _gemini_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            letter = await asyncio.wait_for(
                asyncio.to_thread(generate_cover_letter, vacancy_text, config, ai_client),
                timeout=GEMINI_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.error("Таймаут Gemini API при генерации письма")
            letter = FALLBACK_COVER_LETTER
        finally:
            _gemini_last_call = time.monotonic()

    # generate_cover_letter сам глотает ошибки и возвращает заглушку, поэтому
    # распознаём фолбэк по содержимому — админу важно знать, что письмо шаблонное.
    is_fallback = not letter or not letter.strip() or letter.strip() == FALLBACK_COVER_LETTER.strip()
    if is_fallback:
        db.increment_counter("gemini_fallback")
        return FALLBACK_COVER_LETTER, True
    return letter, False


def build_keyboard(chat_id, msg_id, has_contact):
    """Кнопки под отчётом. chat_id зашит в callback_data, т.к. message.id уникален
    только в пределах одного канала — одинаковые id из разных каналов иначе
    конфликтуют."""
    suffix = f"{chat_id}_{msg_id}"
    rows = []
    # Кнопку отправки не рисуем вовсе, если контакта нет: раньше она была всегда,
    # и нажатие просто выдавало ошибку — бессмысленный клик.
    if has_contact:
        rows.append([InlineKeyboardButton("🚀 Отправить отклик", callback_data=f"send_{suffix}")])
    rows.append([InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{suffix}")])
    return InlineKeyboardMarkup(rows)


def escape_fit(raw, budget):
    """Возвращает (escaped_text, было_ли_урезание), где escaped_text не длиннее budget.

    Обрезать надо СЫРОЙ текст, а мерить — ЭКРАНИРОВАННЫЙ. Экранирование
    раздувает длину до 5x (`&` -> `&amp;`), поэтому простой срез по сырой длине
    не гарантирует лимит. Срезать же уже экранированный текст нельзя: разрезанная
    посередине сущность (`&am`) сломает разбор на стороне Telegram.
    """
    if budget <= 0 or not raw:
        return "", bool(raw)

    cut = raw[:budget]
    escaped = html.escape(cut)
    while len(escaped) > budget and cut:
        # Сжимаем пропорционально перерасходу — сходится за пару итераций.
        cut = cut[:max(0, int(len(cut) * budget / len(escaped)) - 1)]
        escaped = html.escape(cut)

    return escaped, len(cut) < len(raw)


def build_report(score, breakdown, vacancy_text, cover_letter, author, is_fallback):
    """Собирает HTML-отчёт, гарантированно укладывающийся в лимит Telegram.

    Используем HTML, а не MarkdownV2: в MarkdownV2 нужно экранировать 18 символов,
    и любой промах в тексте вакансии (её пишут люди, там любые символы) валит
    запрос с `Can't parse entities`, то есть вакансия просто теряется.
    В HTML экранируются всего три символа.
    """
    breakdown_str = ", ".join(breakdown) if breakdown else "нет совпадений"

    if author:
        target_line = f"Отправить отклик контакту <b>@{html.escape(author)}</b>?"
    else:
        target_line = "⚠️ <b>Контакт не найден</b> — в тексте вакансии нет @username, отправить нельзя."

    fallback_note = (
        "\n⚠️ <i>Gemini недоступен, письмо шаблонное.</i>" if is_fallback else ""
    )

    header = (
        f"🎯 <b>Найдена подходящая вакансия!</b>\n"
        f"Оценка: <b>{score}</b> ({html.escape(breakdown_str)})\n\n"
    )

    # Письмо приоритетнее текста вакансии: именно его админ вычитывает перед
    # отправкой. Но ограничение нужно и ему: модель может вернуть простыню
    # вместо 100 слов, и тогда отчёт не уйдёт вообще.
    letter_esc, letter_cut = escape_fit(cover_letter, 1800)
    letter_block = (
        f"\n\n<b>Сгенерированное письмо:</b>\n{letter_esc}{'…' if letter_cut else ''}"
        f"{fallback_note}\n\n{target_line}"
    )

    original_label = "<b>Оригинал:</b>\n"
    # Остаток лимита отдаём тексту вакансии, но не больше 700 символов —
    # отчёт должен оставаться читаемым с телефона.
    budget = TELEGRAM_MSG_LIMIT - len(header) - len(letter_block) - len(original_label) - 1
    budget = max(0, min(700, budget))

    original_esc, original_cut = escape_fit(vacancy_text, budget)
    original_block = f"{original_label}{original_esc}{'…' if original_cut else ''}"

    return header + original_block + letter_block


async def send_vacancy_to_admin(bot: Bot, message, score, breakdown, cover_letter, author, is_fallback):
    """Отправляет отчёт о вакансии в чат администратора через Bot API."""
    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=build_report(score, breakdown, message.text or "", cover_letter, author, is_fallback),
        parse_mode=ParseMode.HTML,
        reply_markup=build_keyboard(message.chat_id, message.id, has_contact=bool(author)),
        disable_web_page_preview=True,
    )


async def handle_message(message, bot: Bot):
    """Основная логика обработки одного сообщения."""
    vacancy_text = message.text or ""

    if not vacancy_text:
        return

    preview = vacancy_text[:80].replace('\n', ' ')
    log.debug(f"📨 Читаю сообщение id={message.id} chat={message.chat_id}: \"{preview}...\"")

    if db.is_processed(message.id, message.chat_id):
        log.debug("   ⏭ Пропуск: уже обработано ранее")
        return

    # Уровень 1: Быстрый фильтр
    if not level_1_filter(vacancy_text, config):
        log.debug("   ❌ Уровень 1: не прошёл фильтр ключевых слов")
        # Без ссылок на конкретные сообщения — объём слишком большой (почти всё,
        # что пишут каналы), для /stats достаточно одного счётчика.
        db.increment_counter("level1_rejected")
        return

    log.info(f"   ✅ Уровень 1 пройден! id={message.id}")

    # Уровень 2: Оценка алгоритмом
    score, is_passed, breakdown = level_2_scoring(vacancy_text, config)
    breakdown_str = ", ".join(breakdown) if breakdown else "нет совпадений"
    channel_username = channel_username_by_chat_id.get(message.chat_id)

    if not is_passed:
        log.info(
            f"   ❌ Уровень 2: набрал {score} баллов ({breakdown_str}), "
            f"нужно {config['filters']['level_2_scoring']['min_pass_score']}"
        )
        db.log_candidate(message.id, message.chat_id, channel_username, score, passed=False)
        return

    log.info(f"   ✅ Уровень 2 пройден! Score={score} ({breakdown_str})")
    # Фиксируем как «найденную вакансию» до генерации письма — если бот упадёт
    # посередине (например, на вызове Gemini), вакансия всё равно учтёна в /stats.
    db.log_candidate(message.id, message.chat_id, channel_username, score, passed=True)

    author = extract_contact(vacancy_text)

    # Уровень 3: Генерация сопроводительного письма
    cover_letter, is_fallback = await generate_letter(vacancy_text)

    # Сохраняем заявку ДО отправки сообщения: иначе между send_message и
    # save_pending есть окно, в котором админ уже видит кнопку, а данных для неё
    # в БД ещё нет — нажатие выдало бы «данные устарели».
    db.save_pending(message.id, message.chat_id, author, cover_letter)

    try:
        await send_vacancy_to_admin(bot, message, score, breakdown, cover_letter, author, is_fallback)
    except Exception as e:
        # Ключевой момент: mark_processed вызывается ТОЛЬКО после успешной
        # доставки отчёта. Раньше метка ставилась до генерации письма, и любая
        # ошибка отправки означала, что вакансия навсегда помечена обработанной
        # и уже никогда не всплывёт — молча терялась.
        db.delete_pending(message.id, message.chat_id)
        log.error(f"Не удалось отправить отчёт админу для message_id={message.id}: {e}")
        return

    db.mark_processed(message.id, message.chat_id)
    log.info(
        f"Вакансия найдена! Score={score} ({breakdown_str}), "
        f"HR={'@' + author if author else 'контакт не найден'}"
    )


# --- Обработчики Bot API ---

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — нужен, чтобы Telegram разрешил боту писать админу первым,
    и чтобы легко узнать свой chat_id при первой настройке."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        await update.effective_message.reply_text(
            f"Это приватный бот.\nВаш chat_id: {chat_id}"
        )
        return
    await update.effective_message.reply_text(
        "✅ Бот на связи. Здесь будут появляться подходящие вакансии с кнопками "
        "«Отправить отклик» / «Пропустить»."
    )


async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ping — быстрая проверка, что сервер жив и Bot API отвечает.
    Не трогает Telethon/БД/Gemini — проверяет только сам факт, что процесс
    жив и добрался до long polling."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    await update.effective_message.reply_text("pong 🏓")


def build_stats_report():
    """Собирает HTML-отчёт для /stats: воронка вакансий, конверсия, топ каналов
    и ссылки на последние отклонённые на Уровне 2 вакансии."""
    s = db.get_stats()

    lines = ["📊 <b>Статистика бота</b>\n"]

    lines.append("<b>Воронка фильтрации</b>")
    lines.append(f"• Отсеяно на Уровне 1 (ключевые слова): <b>{s['level1_rejected']}</b>")
    lines.append(f"• Дошло до Уровня 2 (оценено алгоритмом): <b>{s['total_candidates']}</b>")
    lines.append(f"• Не прошли Уровень 2 (мало баллов): <b>{s['rejected_level2']}</b>")
    lines.append(f"• Прошли Уровень 2 (найдены): <b>{s['passed_level2']}</b>")

    if s["total_candidates"]:
        conversion = round(100 * s["passed_level2"] / s["total_candidates"], 1)
        lines.append(f"• Конверсия Уровень 1 → найдено: <b>{conversion}%</b>")

    lines.append("\n<b>Что с найденными (прошли Уровень 2)</b>")
    lines.append(f"• 🚀 Отправлено откликов: <b>{s['sent']}</b>")
    lines.append(f"• ❌ Пропущено: <b>{s['skipped']}</b>")
    lines.append(f"• ⏳ Ждёт решения: <b>{s['awaiting_decision']}</b>")

    if s["avg_score"] is not None:
        lines.append(f"\nСредний балл Level 2 у найденных: <b>{s['avg_score']}</b>")

    if s["top_channels"]:
        lines.append("\n<b>Топ каналов по найденным вакансиям</b>")
        for uname, cnt in s["top_channels"]:
            label = f"@{html.escape(uname)}" if uname != "?" else "неизвестно"
            lines.append(f"• {label}: <b>{cnt}</b>")

    if s["gemini_fallback"]:
        lines.append(f"\n⚠️ Писем по шаблону из-за сбоев Gemini: <b>{s['gemini_fallback']}</b>")

    if s["recent_rejected"]:
        lines.append(f"\n<b>Последние отклонённые на Уровне 2</b> (последние {len(s['recent_rejected'])}):")
        for row in s["recent_rejected"]:
            link = message_link(row["chat_id"], row["message_id"])
            label = f"@{html.escape(row['channel_username'])}" if row["channel_username"] else "канал неизвестен"
            lines.append(f'• <a href="{link}">{label}, {row["score"]} баллов</a>')

    return "\n".join(lines)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — воронка вакансий: сколько отсеялось на каждом этапе, сколько
    отправлено/пропущено, и ссылки на последние отклонённые на Уровне 2 вакансии."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    await update.effective_message.reply_text(
        build_stats_report(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Принимаем нажатия только от админа. Кто-то может переслать сообщение с
    # кнопками, и тогда чужой клик отправил бы резюме от вашего имени.
    if query.from_user.id != ADMIN_CHAT_ID:
        log.warning(f"Отклонено нажатие от чужого пользователя id={query.from_user.id}")
        await query.answer("Нет доступа.", show_alert=True)
        return

    parts = (query.data or "").split("_", 2)
    if len(parts) != 3:
        await query.answer()
        return

    action, chat_id_raw, msg_id_raw = parts
    try:
        chat_id = int(chat_id_raw)
        msg_id = int(msg_id_raw)
    except ValueError:
        await query.answer()
        return

    if action == "skip":
        await query.answer()
        db.delete_pending(msg_id, chat_id)
        db.set_vacancy_outcome(msg_id, chat_id, "skipped")
        await safe_edit(query, "❌ Отклик пропущен.")
        log.info(f"Отклик пропущен: msg_id={msg_id} chat_id={chat_id}")
        return

    if action != "send":
        await query.answer()
        return

    # Забираем заявку атомарно. Два быстрых клика по «Отправить» раньше могли
    # пройти оба и отправить HR два резюме — claim_pending отдаёт запись
    # ровно одному обработчику.
    app_data = db.claim_pending(msg_id, chat_id)
    if not app_data:
        # answer() с алертом вызываем ПЕРВЫМ и единственным разом: повторный
        # answer() по тому же callback_query Telegram отклоняет, поэтому раньше
        # предупреждение «данные устарели» вообще не показывалось.
        await query.answer("Заявка уже обработана или устарела.", show_alert=True)
        await safe_edit(query, "⚠️ Заявка уже обработана или устарела.")
        return

    author = app_data["author"]
    cover_letter = app_data["cover_letter"]

    if not author:
        await query.answer(
            "Невозможно отправить: в тексте вакансии не найден @username.",
            show_alert=True,
        )
        # Заявку возвращаем — она всё ещё может быть полезна, и админ сможет
        # нажать «Пропустить» осознанно.
        db.save_pending(msg_id, chat_id, author, cover_letter)
        return

    await query.answer()
    # Убираем кнопки сразу, чтобы админ физически не мог нажать второй раз,
    # пока идёт отправка (она занимает несколько секунд).
    await safe_edit(query, f"⏳ Отправляю отклик @{html.escape(author)}…")

    try:
        async with client.action(author, 'typing'):
            await asyncio.sleep(2)

        await client.send_file(author, file=RESUME_PATH, caption=cover_letter)
    except Exception as e:
        log.error(f"Ошибка отправки @{author}: {e}")
        # Возвращаем заявку в БД и кнопки в сообщение: отправка не удалась, но
        # письмо уже сгенерировано — админ может повторить попытку, а не терять
        # вакансию из-за разовой сетевой ошибки.
        db.save_pending(msg_id, chat_id, author, cover_letter)
        await safe_edit(
            query,
            f"❌ Не удалось отправить @{html.escape(author)}:\n<code>{html.escape(str(e))}</code>\n\n"
            f"Можно повторить попытку.",
            reply_markup=build_keyboard(chat_id, msg_id, has_contact=True),
        )
        return

    db.set_vacancy_outcome(msg_id, chat_id, "sent")
    await safe_edit(query, f"✅ Отклик успешно отправлен @{html.escape(author)}.")
    log.info(f"Отклик отправлен: @{author}")


async def safe_edit(query, text, reply_markup=None):
    """edit_message_text, который не роняет обработчик.

    Правка сообщения может не пройти (сообщение удалено, текст не изменился,
    сеть моргнула). Раньше такое исключение всплывало наружу уже ПОСЛЕ
    отправки резюме и попадало в лог как ошибка отправки, хотя резюме ушло.
    """
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except TelegramError as e:
        log.warning(f"Не удалось обновить сообщение с кнопками: {e}")


async def error_handler(update, context):
    """Без этого исключение в обработчике PTB уходит в его внутренний логгер, и
    в bot.log не остаётся ни стектрейса, ни контекста."""
    log.error("Необработанная ошибка в обработчике Bot API", exc_info=context.error)


async def authorize_telethon():
    """Авторизация юзер-сессии по QR-коду при первом запуске."""
    if await client.is_user_authorized():
        return True

    log.info("Сессия не найдена. Создаем новую через QR-код...")
    print("\n" + "=" * 50)
    print("❗ Сессия не найдена. Создаем новую через QR-код...")
    qr_login = await client.qr_login()

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_login.url)
    qr.make(fit=True)

    print("\n=== КАК ВОЙТИ ===")
    print("1. Откройте приложение Telegram на телефоне.")
    print("2. Перейдите в Настройки -> Устройства -> Подключить устройство.")
    print("3. Отсканируйте этот QR-код:\n")

    qr.print_ascii(invert=True)

    try:
        await qr_login.wait(timeout=120)
        log.info("Успешная авторизация!")
        return True
    except Exception as e:
        log.error(f"Ошибка авторизации: {e}")
        return False


def build_ptb_application():
    """Собирает Application для Bot API, с маршрутизацией через Cloudflare Worker,
    если он включён (network.mode == "cloudflare_worker").

    python-telegram-bot держит ДВА отдельных HTTP-клиента внутри Bot:
    один для обычных вызовов (sendMessage и т.д.), другой — только для
    getUpdates при long polling. Если настроить только .request(), второй клиент
    молча создаётся с дефолтными настройками без заголовка X-Proxy-Token — и
    long polling по-прежнему будет биться о ConnectTimeout напрямую в api.telegram.org.
    Поэтому задаём оба клиента явно, с одинаковыми заголовками.
    """
    builder = Application.builder().token(ADMIN_BOT_TOKEN)

    if ptb_base_url:
        builder = builder.base_url(ptb_base_url)
        builder = builder.request(HTTPXRequest(httpx_kwargs={"headers": ptb_proxy_headers}))
        builder = builder.get_updates_request(HTTPXRequest(httpx_kwargs={"headers": ptb_proxy_headers}))

    return builder.build()


async def main(mode="normal"):
    await client.connect()

    if not await authorize_telethon():
        return

    ptb_app = build_ptb_application()
    ptb_app.add_handler(CommandHandler("start", start_handler))
    ptb_app.add_handler(CommandHandler("ping", ping_handler))
    ptb_app.add_handler(CommandHandler("stats", stats_handler))
    ptb_app.add_handler(CallbackQueryHandler(button_handler))
    ptb_app.add_error_handler(error_handler)

    await ptb_app.initialize()
    bot: Bot = ptb_app.bot
    # Даём notify_admin/TelegramAlertHandler доступ к уже запущенному боту, чтобы не
    # создавать отдельный httpx-клиент на каждый алерт.
    _admin_bot_ref["bot"] = bot

    # Стартовое уведомление — не «приятный бонус», а проверка живости канала
    # доставки. Если бот не может писать админу, вся фича мертва: вакансии будут
    # отбираться и молча пропадать. Поэтому падаем сразу и с инструкцией.
    startup_text = f"✅ Бот запущен (режим: {mode})."
    if RESUME_MISSING:
        startup_text += (
            f"\n\n⚠️ <b>Резюме не найдено</b>: <code>{html.escape(RESUME_PATH)}</code>\n"
            f"Отправка откликов не заработает, пока файл не появится."
        )

    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=startup_text, parse_mode=ParseMode.HTML)
    except Forbidden:
        await ptb_app.shutdown()
        raise SystemExit(
            f"Бот не может писать в чат {ADMIN_CHAT_ID}.\n"
            f"Откройте диалог с ботом в Telegram и нажмите /start, затем перезапустите. "
            f"Если admin_bot.chat_id указан неверно — исправьте его в config.yaml."
        )
    except TelegramError as e:
        await ptb_app.shutdown()
        raise SystemExit(f"Bot API недоступен ({e}). Проверьте admin_bot.bot_token и сеть.")

    await ptb_app.start()
    await ptb_app.updater.start_polling(
        drop_pending_updates=True,
        timeout=50
    )

    # Нужно для ссылок на сообщения в /stats и отчётах о вакансиях.
    await build_channel_username_map()

    # Обработчик регистрируем только после того, как канал доставки проверен.
    @client.on(events.NewMessage(chats=config["telegram"]["channels"]))
    async def process_new_vacancy(event):
        try:
            await handle_message(event.message, bot)
        except Exception:
            # Исключение в обработчике Telethon гасится библиотекой почти
            # бесследно — логируем со стектрейсом сами.
            log.exception(f"Ошибка обработки сообщения id={event.message.id}")

    try:
        if mode == "yest":
            log.info("Режим --mode=yest: Собираем вакансии за вчера и сегодня...")

            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            yesterday_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

            for channel in config["telegram"]["channels"]:
                log.info(f"📡 Парсинг истории канала: {channel}")
                try:
                    async for msg in client.iter_messages(channel, offset_date=yesterday_start, reverse=True):
                        try:
                            await handle_message(msg, bot)
                        except Exception:
                            # Одна битая вакансия не должна обрывать разбор
                            # всего канала (а раньше — и всех следующих каналов).
                            log.exception(f"Ошибка обработки истории id={msg.id}")
                        await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning(f"Ошибка доступа к каналу {channel}: {e}")

            log.info("Исторические вакансии обработаны!")

        log.info("Бот запущен. Мониторим новые вакансии в реальном времени...")
        await client.run_until_disconnected()
    finally:
        log.info("Останавливаю Admin Bot...")
        if ptb_app.updater.running:
            await ptb_app.updater.stop()
        if ptb_app.running:
            await ptb_app.stop()
        await ptb_app.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram Vacancy Bot")
    parser.add_argument(
        "--mode",
        choices=["normal", "yest"],
        default="normal",
        help="normal = только новые; yest = вчера+сегодня, потом мониторинг",
    )
    args = parser.parse_args()

    try:
        client.loop.run_until_complete(main(mode=args.mode))
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
    except Exception:
        # log.exception уже уйдёт через TelegramAlertHandler, НО только если в
        # момент вызова ещё есть запущенный event loop — а к этому моменту
        # run_until_complete уже завершился и логировать в нём больше некуда. Поэтому
        # явно шлём алерт ещё раз через тот же (всё ещё живой) loop.
        log.exception("Бот упал с необработанным исключением")
        try:
            crash_text = (
                f"🔴 <b>Бот упал и остановился</b>\n"
                f"<pre>{html.escape(traceback.format_exc()[-3500:])}</pre>"
            )
            client.loop.run_until_complete(notify_admin(crash_text))
        except Exception:
            pass
        raise
        