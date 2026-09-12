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
from models.job import Job
from models.candidate import CandidateProfile
from models.application import ApplicationDraft
from providers.telegram import TelegramJobProvider
from providers.indeed import IndeedJobProvider
from applications.telegram import TelegramApplicationService
from applications.indeed import IndeedApplicationService
from services.job_pipeline import dedup_and_store, passes_level1, score_job
from services.gemini_throttle import gemini_throttle
from services.contact_extraction import extract_contact as _extract_contact_impl

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

# --- Несколько резюме (resumes:) — обратная совместимость с одиночным
# user_profile.resume_path, если блок resumes не задан вовсе. ---
_resumes_cfg = config.get("resumes")
if _resumes_cfg:
    RESUMES = {
        name: (path if os.path.isabs(path) else os.path.join(BASE_DIR, path))
        for name, path in _resumes_cfg.items()
    }
    RESUMES.setdefault("default", next(iter(RESUMES.values())))
else:
    RESUMES = {"default": RESUME_PATH}

# --- Профиль кандидата для AI structured analysis (см. models/candidate.py) ---
candidate_profile = CandidateProfile.from_config(config.get("candidate"))

# --- Источники вакансий: telegram (событийный, как раньше) + indeed
# (опциональный, опрашивается по расписанию). По умолчанию telegram включён,
# indeed выключен — старое поведение бота не меняется, пока сам не включите. ---
_sources_cfg = config.get("sources") or {}
_telegram_source_cfg = _sources_cfg.get("telegram") or {}
_indeed_source_cfg = _sources_cfg.get("indeed") or {}
TELEGRAM_SOURCE_ENABLED = _telegram_source_cfg.get("enabled", True)
INDEED_SOURCE_ENABLED = _indeed_source_cfg.get("enabled", False)
INDEED_POLL_INTERVAL_SEC = int(_indeed_source_cfg.get("poll_interval_sec", 1800))

# Общий rate-limiter Gemini (см. services/gemini_throttle.py) — используется и
# для писем Telegram (generate_letter), и для structured analysis (Indeed).
gemini_throttle.min_interval_sec = GEMINI_MIN_INTERVAL_SEC
gemini_throttle.timeout_sec = GEMINI_TIMEOUT_SEC

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

# --- Job providers / application services (см. providers/, applications/) ---
# TelegramJobProvider.to_job() использует тот же channel_username_by_chat_id,
# который заполняется позже в build_channel_username_map — это тот же словарь по
# ссылке (мутируется на месте), не копия.
telegram_provider = TelegramJobProvider(client, config["telegram"]["channels"], channel_username_by_chat_id)
telegram_app_service = TelegramApplicationService(client, channel_usernames, RESUMES, config, ai_client)

indeed_provider = None
indeed_app_service = None
if INDEED_SOURCE_ENABLED:
    _adzuna_cfg = _indeed_source_cfg.get("adzuna") or {}
    indeed_provider = IndeedJobProvider(
        search_queries=_indeed_source_cfg.get("search_queries") or [],
        locations=_indeed_source_cfg.get("locations") or [],
        app_id=_adzuna_cfg.get("app_id", ""),
        app_key=_adzuna_cfg.get("app_key", ""),
        country=_adzuna_cfg.get("country", ""),
    )
    indeed_app_service = IndeedApplicationService(config, ai_client, RESUMES)
    log.info(
        f"Источник Indeed включён, опрос каждые {INDEED_POLL_INTERVAL_SEC}с "
        f"через Adzuna API (country={indeed_provider.country!r}). Это НЕ сам Indeed — "
        f"у Indeed нет официального API для этого, см. providers/indeed.py."
    )


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

def extract_contact(text):
    """Ищет юзернейм HR в тексте вакансии. Возвращает None, если не нашёл.

    Перенесено в services/contact_extraction.py, чтобы applications/telegram.py могло
    переиспользовать ту же логику без циклического импорта; эта функция оставлена
    как тонкая обёртка, чтобы не менять вызовы по всему main.py.
    """
    return _extract_contact_impl(text, channel_usernames)


async def generate_letter(vacancy_text):
    """Генерирует письмо. Возвращает (letter, is_fallback).

    generate_cover_letter делает синхронный сетевой запрос к Gemini — если вызвать
    его напрямую, он блокирует весь event loop (все каналы, все кнопки) на время
    запроса. Уносим в отдельный поток и ограничиваем таймаутом через общий
    gemini_throttle (см. services/gemini_throttle.py) — общий с Indeed structured
    analysis, чтобы оба источника не удваивали реальный RPS к Gemini.
    """
    try:
        letter = await gemini_throttle.run(generate_cover_letter, vacancy_text, config, ai_client)
    except asyncio.TimeoutError:
        log.error("Таймаут Gemini API при генерации письма")
        letter = FALLBACK_COVER_LETTER

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

    # Общая таблица jobs (см. models/job.py, services/job_pipeline.py) — дописывается рядом
    # с vacancy_log в тех же точках, ничего в старом поведении не меняет — нужна только для
    # единой статистики по /stats в разрезе источника.
    job = telegram_provider.to_job(message)

    if not is_passed:
        log.info(
            f"   ❌ Уровень 2: набрал {score} баллов ({breakdown_str}), "
            f"нужно {config['filters']['level_2_scoring']['min_pass_score']}"
        )
        db.log_candidate(message.id, message.chat_id, channel_username, score, passed=False)
        dedup_and_store(db, job, score=score, status="rejected")
        return

    log.info(f"   ✅ Уровень 2 пройден! Score={score} ({breakdown_str})")
    # Фиксируем как «найденную вакансию» до генерации письма — если бот упадёт
    # посередине (например, на вызове Gemini), вакансия всё равно учтёна в /stats.
    db.log_candidate(message.id, message.chat_id, channel_username, score, passed=True)
    dedup_and_store(db, job, score=score, status="qualified")

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

    # --- Статистика по источникам (jobs/applications, см. database.py::get_source_stats).
    # Считана с момента внедрения Job-абстракции, не заменяет воронку выше. ---
    src_stats = db.get_source_stats()
    lines.append("\n\n<b>📊 По источникам</b>")
    for source, label in (("telegram", "📨 Telegram"), ("indeed", "🌐 Indeed")):
        st = src_stats[source]
        lines.append(f"\n{label}")
        lines.append(f"Found: <b>{st['found']}</b>")
        lines.append(f"Qualified: <b>{st['qualified']}</b>")
        lines.append(f"Applied: <b>{st['applied']}</b>")
        lines.append(f"Skipped: <b>{st['skipped']}</b>")
        if st["qualified"]:
            conv = round(100 * st["applied"] / st["qualified"], 1)
            lines.append(f"Application conversion: <b>{conv}%</b>")

    if not INDEED_SOURCE_ENABLED:
        lines.append("\n<i>(Indeed выключён — sources.indeed.enabled: false)</i>")

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
        _job_id = db.get_job_id_by_dedup_key(f"telegram:{chat_id}:{msg_id}")
        if _job_id:
            db.set_job_status(_job_id, "skipped")
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

    # Фактическая отправка через TelegramApplicationService.submit() (applications/telegram.py) —
    # то же самое client.action+send_file, теперь за ApplicationService абстракцией.
    # Вся остальная оркестрация (claim_pending/retry/safe_edit) не тронута.
    draft = ApplicationDraft(
        job=Job(source="telegram", external_id=f"{chat_id}:{msg_id}"),
        candidate=candidate_profile,
        cover_letter=cover_letter,
        resume_path=RESUME_PATH,
        contact=author,
    )
    result = await telegram_app_service.submit(draft)
    if not result.success:
        log.error(f"Ошибка отправки @{author}: {result.message}")
        # Возвращаем заявку в БД и кнопки в сообщение: отправка не удалась, но
        # письмо уже сгенерировано — админ может повторить попытку, а не терять
        # вакансию из-за разовой сетевой ошибки.
        db.save_pending(msg_id, chat_id, author, cover_letter)
        await safe_edit(
            query,
            f"❌ Не удалось отправить @{html.escape(author)}:\n<code>{html.escape(result.message)}</code>\n\n"
            f"Можно повторить попытку.",
            reply_markup=build_keyboard(chat_id, msg_id, has_contact=True),
        )
        return

    db.set_vacancy_outcome(msg_id, chat_id, "sent")
    _job_id = db.get_job_id_by_dedup_key(f"telegram:{chat_id}:{msg_id}")
    if _job_id:
        db.set_job_status(_job_id, "applied")
        _app_id = db.create_application(_job_id, status="applied", resume_name="default", cover_letter=cover_letter)
        db.update_application_status(_app_id, "applied", mark_applied=True)
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


# --- Indeed: отдельный UI/pipeline (см. providers/indeed.py, applications/indeed.py) ---
# Напоминание: submit() для Indeed НИКОГДА не подаёт заявку автоматически —
# официального API для этого нет. Админ открывает ссылку и подтверждает подачу вручную.

def build_indeed_report(draft: ApplicationDraft) -> str:
    """HTML-отчёт по вакансии Indeed для админа — формат согласован отдельно от
    Telegram-отчёта (build_report), чтобы не трогать его."""
    job = draft.job
    lines = [f"🔥 <b>{html.escape(job.title or '(без названия)')}</b>\n"]
    if job.company:
        lines.append(f"🏢 {html.escape(job.company)}")
    if job.location:
        lines.append(f"📍 {html.escape(job.location)}")
    if job.salary:
        lines.append(f"💰 {html.escape(job.salary)}")

    lines.append(f"\n🎯 Match: <b>{draft.match_score}%</b> ({html.escape(draft.recommendation)})")

    if draft.strengths:
        lines.append("\n✅ " + "; ".join(html.escape(s) for s in draft.strengths))
    if draft.missing_requirements:
        lines.append("\n⚠️ " + "; ".join(html.escape(s) for s in draft.missing_requirements))
    if draft.risks:
        lines.append("\n🚩 " + "; ".join(html.escape(s) for s in draft.risks))

    letter_esc, letter_cut = escape_fit(draft.cover_letter, 1200)
    lines.append(f"\n🤖 <b>Cover letter:</b>\n{letter_esc}{'…' if letter_cut else ''}")
    if draft.is_ai_fallback:
        lines.append("⚠️ <i>Gemini недоступен, письмо/анализ шаблонные.</i>")

    if draft.screener_questions:
        lines.append("\n❓ <b>Возможные вопросы анкеты:</b>")
        for q in draft.screener_questions[:8]:
            mark = "⚠️ требует подтверждения" if q.requires_confirmation else f"conf={q.confidence:.1f}"
            answer = html.escape(q.answer) if q.answer else "—"
            lines.append(f"• {html.escape(q.question)}\n  → {answer} ({mark})")

    resume_file = os.path.basename(draft.resume_path or draft.resume_name)
    lines.append(f"\n📄 Resume: <code>{html.escape(resume_file)}</code>")

    return "\n".join(lines)[:TELEGRAM_MSG_LIMIT]


def build_indeed_keyboard(job_id, application_id, job_url):
    suffix = f"{job_id}_{application_id}"
    rows = []
    if job_url:
        rows.append([InlineKeyboardButton("🚀 Apply on Indeed", url=job_url)])
    rows.append([
        InlineKeyboardButton("✅ Я откликнулся", callback_data=f"indeed_applied_{suffix}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"indeed_skip_{suffix}"),
    ])
    rows.append([
        InlineKeyboardButton("📝 Переписать письмо", callback_data=f"indeed_editletter_{suffix}"),
        InlineKeyboardButton("📄 Сменить резюме", callback_data=f"indeed_resume_{suffix}"),
    ])
    return InlineKeyboardMarkup(rows)


async def process_indeed_job(job: Job, bot: Bot):
    """Один элемент опроса Indeed: dedup -> level1/level2 -> AI structured analysis ->
    отчёт админу. Никогда не подаёт заявку самостоятельно."""
    job_id, is_new = dedup_and_store(db, job, status="discovered")
    if not is_new:
        log.debug(f"Indeed: вакансия {job.id} уже видели раньше, пропускаю")
        return

    if not passes_level1(job, config):
        db.set_job_status(job_id, "rejected")
        return

    score, is_passed, breakdown = score_job(job, config)
    if not is_passed:
        db.update_job_score_status(job_id, score, "rejected")
        return

    db.update_job_score_status(job_id, score, "qualified")

    try:
        draft = await indeed_app_service.prepare(job, candidate_profile)
    except Exception:
        log.exception(f"Сбой подготовки Indeed-заявки для job_id={job_id}")
        return

    application_id = db.create_application(
        job_id, status="ready", resume_name=draft.resume_name, cover_letter=draft.cover_letter
    )
    db.save_screener_answers(application_id, [
        {"question": q.question, "answer": q.answer, "confidence": q.confidence,
         "source": q.source, "requires_confirmation": q.requires_confirmation}
        for q in draft.screener_questions
    ])

    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=build_indeed_report(draft),
            parse_mode=ParseMode.HTML,
            reply_markup=build_indeed_keyboard(job_id, application_id, job.url),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"Не удалось отправить Indeed-отчёт админу (job_id={job_id}): {e}")


def _current_message_text(query) -> str:
    """query.message может быть None/InaccessibleMessage (сообщению >48ч или удалено) —
    тогда у него нет .text_html/.text. Возвращаем пустую строку вместо падения."""
    message = getattr(query, "message", None)
    if message is None:
        return ""
    return getattr(message, "text_html", None) or getattr(message, "text", None) or ""


async def indeed_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        log.warning(f"Отклонено нажатие (Indeed) от чужого пользователя id={query.from_user.id}")
        await query.answer("Нет доступа.", show_alert=True)
        return

    parts = (query.data or "").split("_")
    if len(parts) != 4:
        await query.answer()
        return
    _, action, job_id_raw, app_id_raw = parts
    try:
        job_id = int(job_id_raw)
        application_id = int(app_id_raw)
    except ValueError:
        await query.answer()
        return

    if action == "skip":
        await query.answer()
        db.set_job_status(job_id, "skipped")
        db.update_application_status(application_id, "skipped")
        await safe_edit(query, "❌ Вакансия Indeed пропущена.")
        return

    if action == "applied":
        await query.answer("Отмечено ✅")
        result = IndeedApplicationService.confirm_applied()
        db.set_job_status(job_id, "applied")
        db.update_application_status(application_id, "applied", mark_applied=True)
        current_text = _current_message_text(query)
        await safe_edit(query, f"{current_text}\n\n✅ <b>{html.escape(result.message)}</b>")
        return

    if action == "editletter":
        await query.answer("Переписываю письмо…")
        job_row = db.get_job_by_id(job_id)
        if not job_row:
            return
        job = Job(
            source=job_row["source"], external_id=job_row["external_id"], title=job_row["title"],
            company=job_row["company"], location=job_row["location"], url=job_row["url"],
            description=job_row["description"],
        )
        try:
            draft = await indeed_app_service.prepare(job, candidate_profile)
        except Exception:
            log.exception(f"Не удалось перегенерировать письмо Indeed для job_id={job_id}")
            return
        db.update_application_cover_letter(application_id, draft.cover_letter)
        await safe_edit(
            query, build_indeed_report(draft),
            reply_markup=build_indeed_keyboard(job_id, application_id, job.url),
        )
        return

    if action == "resume":
        await query.answer()
        app_row = db.get_application(application_id)
        if not app_row or not RESUMES:
            return
        names = list(RESUMES.keys())
        try:
            idx = names.index(app_row["resume_name"])
        except ValueError:
            idx = -1
        next_name = names[(idx + 1) % len(names)]
        db.update_application_resume(application_id, next_name)
        resume_file = os.path.basename(RESUMES[next_name])
        current_text = _current_message_text(query)
        new_text = re.sub(
            r"📄 Resume: <code>.*?</code>",
            f"📄 Resume: <code>{html.escape(resume_file)}</code>",
            current_text,
        )
        reply_markup = getattr(query.message, "reply_markup", None) if query.message else None
        await safe_edit(query, new_text, reply_markup=reply_markup)
        return

    await query.answer()


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
    ptb_app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(send|skip)_"))
    ptb_app.add_handler(CallbackQueryHandler(indeed_button_handler, pattern=r"^indeed_"))
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
    # sources.telegram.enabled позволяет полностью выключить Telegram как источник
    # вакансий (по умолчанию включён — поведение не меняется).
    if TELEGRAM_SOURCE_ENABLED:
        @client.on(events.NewMessage(chats=config["telegram"]["channels"]))
        async def process_new_vacancy(event):
            try:
                await handle_message(event.message, bot)
            except Exception:
                # Исключение в обработчике Telethon гасится библиотекой почти
                # бесследно — логируем со стектрейсом сами.
                log.exception(f"Ошибка обработки сообщения id={event.message.id}")
    else:
        log.info("Источник Telegram выключён (sources.telegram.enabled: false) — каналы не опрашиваются.")

    # Indeed-пайплайн: opt-in, опрашивает фид с интервалом INDEED_POLL_INTERVAL_SEC.
    # Не блокирует старт бота, если фид не настроен (см. providers/indeed.py).
    indeed_poll_task = None
    if INDEED_SOURCE_ENABLED:
        async def indeed_poll_loop():
            while True:
                try:
                    jobs = await indeed_provider.fetch_jobs()
                    log.debug(f"Indeed: получено {len(jobs)} вакансий из фида")
                    for job in jobs:
                        try:
                            await process_indeed_job(job, bot)
                        except Exception:
                            log.exception(f"Ошибка обработки Indeed-вакансии {job.id}")
                except Exception:
                    log.exception("Ошибка опроса Indeed")
                await asyncio.sleep(INDEED_POLL_INTERVAL_SEC)

        indeed_poll_task = asyncio.create_task(indeed_poll_loop())

    try:
        if mode == "yest" and TELEGRAM_SOURCE_ENABLED:
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
        if indeed_poll_task is not None:
            indeed_poll_task.cancel()
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
        