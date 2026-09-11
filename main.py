import argparse
import asyncio
import logging
import os
import yaml
import qrcode
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.tl.custom import Button
from google.genai import types as genai_types
from database import db
from filters import level_1_filter, level_2_scoring
from ai_generator import generate_cover_letter, create_ai_client, FALLBACK_COVER_LETTER

# Пути считаются от расположения скрипта, а не от текущей рабочей директории —
# иначе запуск через systemd/cron с другим WorkingDirectory тихо создаёт
# новую сессию/БД/лог в неожиданном месте.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- #13: Нормальное логирование вместо print() ---
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

# --- #8: Конфиг грузится ОДИН раз ---
with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# --- Режим сети: "direct" (как раньше) или "cloudflare_worker" ---
# Нужен, когда хостинг бота не имеет нормального прямого доступа к Telegram
# и/или Google (например, сервер в РФ). В режиме cloudflare_worker и MTProto
# (Telethon), и запросы к Gemini идут через Cloudflare Worker — см.
# cloudflare_transport.py и cloudflare-worker/README.md.
network_cfg = config.get("network", {"mode": "direct"})
network_mode = network_cfg.get("mode", "direct")

telethon_kwargs = {}
gemini_http_options = None

if network_mode == "cloudflare_worker":
    cf_cfg = network_cfg["cloudflare_worker"]
    worker_base = cf_cfg["base_url"].rstrip("/")
    proxy_token = cf_cfg["proxy_token"]

    if worker_base.startswith("https://"):
        ws_base = "wss://" + worker_base[len("https://"):]
    elif worker_base.startswith("http://"):
        ws_base = "ws://" + worker_base[len("http://"):]
    else:
        raise ValueError(f"network.cloudflare_worker.base_url должен начинаться с http:// или https://: {worker_base!r}")

    from cloudflare_transport import make_cloudflare_connection
    telethon_kwargs["connection"] = make_cloudflare_connection(
        ws_url=f"{ws_base}/mtproto",
        token=proxy_token,
    )
    gemini_http_options = genai_types.HttpOptions(
        base_url=f"{worker_base}/gemini",
        headers={"X-Proxy-Token": proxy_token},
    )
    log.info(f"Режим сети: cloudflare_worker ({worker_base})")
else:
    log.info("Режим сети: direct")

# Инициализация клиентов
client = TelegramClient(
    os.path.join(BASE_DIR, "bot_session"),
    config["telegram"]["api_id"],
    config["telegram"]["api_hash"],
    **telethon_kwargs
)
ai_client = create_ai_client(config["ai"]["gemini_api_key"], http_options=gemini_http_options)

# Множество юзернеймов каналов (lower) для фильтрации в extract_contact (#10)
channel_usernames = {ch.lower() for ch in config["telegram"]["channels"]}

def extract_contact(text):
    """Ищет юзернеймы HR в тексте вакансии, игнорируя юзернеймы каналов."""
    usernames = re.findall(r'@([A-Za-z0-9_]{5,32})', text)
    for username in usernames:
        # #10: Пропускаем юзернеймы каналов, чтобы не писать самому каналу
        if username.lower() not in channel_usernames:
            return username
    return None

async def handle_message(message):
    """Основная логика обработки одного сообщения."""
    vacancy_text = message.text or ""
    
    if not vacancy_text:
        return
    
    # Показываем превью каждого прочитанного сообщения
    preview = vacancy_text[:80].replace('\n', ' ')
    log.debug(f"📨 Читаю сообщение id={message.id} chat={message.chat_id}: \"{preview}...\"")
        
    if db.is_processed(message.id, message.chat_id):
        log.debug(f"   ⏭ Пропуск: уже обработано ранее")
        return
    
    # Уровень 1: Быстрый фильтр
    if not level_1_filter(vacancy_text, config):
        log.debug(f"   ❌ Уровень 1: не прошёл фильтр ключевых слов")
        return
    
    log.info(f"   ✅ Уровень 1 пройден! id={message.id}")
        
    # Уровень 2: Оценка алгоритмом (теперь с расшифровкой)
    score, is_passed, breakdown = level_2_scoring(vacancy_text, config)
    breakdown_str = ", ".join(breakdown) if breakdown else "нет совпадений"
    
    if not is_passed:
        log.info(f"   ❌ Уровень 2: набрал {score} баллов ({breakdown_str}), нужно {config['filters']['level_2_scoring']['min_pass_score']}")
        return
    
    log.info(f"   ✅ Уровень 2 пройден! Score={score} ({breakdown_str})")
        
    db.mark_processed(message.id, message.chat_id)
    
    hr_contact = extract_contact(vacancy_text)
    author = hr_contact if hr_contact else "Контакт не найден"
    
    # #12: Задержка между запросами к Gemini API (rate limiting)
    await asyncio.sleep(1)

    # Уровень 3: Генерация сопроводительного письма.
    # generate_cover_letter делает синхронный сетевой запрос к Gemini — если вызвать
    # его напрямую, он блокирует весь event loop (все каналы, все кнопки) на время
    # запроса. Уносим в отдельный поток и ограничиваем таймаутом.
    try:
        cover_letter = await asyncio.wait_for(
            asyncio.to_thread(generate_cover_letter, vacancy_text, config, ai_client),
            timeout=30
        )
    except asyncio.TimeoutError:
        log.error("Таймаут Gemini API при генерации письма")
        cover_letter = FALLBACK_COVER_LETTER

    # Кнопки для Избранного. chat_id зашит в data, т.к. message.id уникален только
    # в пределах одного канала — одинаковые id из разных каналов иначе конфликтуют.
    keyboard = [
        [Button.inline("🚀 Отправить отклик", data=f"send_{message.chat_id}_{message.id}".encode('utf-8'))],
        [Button.inline("❌ Пропустить", data=f"skip_{message.chat_id}_{message.id}".encode('utf-8'))]
    ]

    # #14: Красивая расшифровка баллов
    breakdown_str = ", ".join(breakdown) if breakdown else "нет совпадений"

    report_text = (
        f"🎯 **Найдена подходящая вакансия!**\n"
        f"Оценка: **{score}** баллов ({breakdown_str})\n\n"
        f"**Оригинал:**\n{vacancy_text[:500]}\n\n"
        f"**Сгенерированное письмо:**\n{cover_letter}\n\n"
        f"Отправить пользователю: @{author}?"
    )

    try:
        await client.send_message(
            "me",
            report_text,
            buttons=keyboard,
            link_preview=False
        )
    except Exception as e:
        # Сообщение уже помечено как processed — если не залогировать явно,
        # вакансия молча теряется без единого следа кроме этой строки в логе.
        log.error(f"Не удалось отправить отчёт в Избранное для message_id={message.id}: {e}")
        return

    # #6: Сохраняем в БД, а не в память
    db.save_pending(message.id, message.chat_id, author, cover_letter)

    log.info(f"Вакансия найдена! Score={score} ({breakdown_str}), HR=@{author}")

@client.on(events.NewMessage(chats=config["telegram"]["channels"]))
async def process_new_vacancy(event):
    await handle_message(event.message)

@client.on(events.CallbackQuery())
async def button_handler(event):
    data = event.data.decode('utf-8')

    parts = data.split("_", 2)
    if len(parts) != 3:
        return

    action, chat_id, msg_id = parts
    chat_id = int(chat_id)
    msg_id = int(msg_id)

    if action == "skip":
        await event.edit("❌ Отклик пропущен.")
        db.delete_pending(msg_id, chat_id)
        log.info(f"Отклик пропущен: msg_id={msg_id}")

    elif action == "send":
        # #6: Читаем из БД — переживёт перезапуск
        app_data = db.get_pending(msg_id, chat_id)
        if not app_data:
            await event.answer("Ошибка: данные устарели.", alert=True)
            return
            
        author = app_data["author"]
        cover_letter = app_data["cover_letter"]
        
        if author == "Контакт не найден":
            await event.answer("Невозможно отправить: в тексте вакансии не найден @username.", alert=True)
            return
            
        try:
            async with client.action(author, 'typing'):
                await asyncio.sleep(2)
            
            resume_path = config["user_profile"]["resume_path"]
            await client.send_file(
                author, 
                file=resume_path, 
                caption=cover_letter
            )
            
            await event.edit(f"✅ Отклик успешно отправлен контакту @{author}.")
            db.delete_pending(msg_id, chat_id)
            log.info(f"Отклик отправлен: @{author}")
            
        except Exception as e:
            log.error(f"Ошибка отправки @{author}: {e}")
            await event.answer(f"Ошибка при отправке: {e}", alert=True)

async def main(mode="normal"):
    await client.connect()
    
    if not await client.is_user_authorized():
        log.info("Сессия не найдена. Создаем новую через QR-код...")
        print("\n" + "="*50)
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
        except Exception as e:
            log.error(f"Ошибка авторизации: {e}")
            return

    # Явное подтверждение в Избранном, что бот жив — если что-то упадёт при
    # старте (например, невалидный канал в конфиге), это сообщение не придёт,
    # и вы сразу поймёте, что бот не поднялся.
    try:
        await client.send_message("me", f"✅ Бот запущен (режим: {mode}).")
    except Exception as e:
        log.warning(f"Не удалось отправить стартовое уведомление: {e}")

    if mode == "yest":
        log.info("Режим --mode=yest: Собираем вакансии за вчера и сегодня...")
        
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        yesterday_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        
        for channel in config["telegram"]["channels"]:
            log.info(f"📡 Парсинг истории канала: {channel}")
            try:
                async for msg in client.iter_messages(channel, offset_date=yesterday_start, reverse=True):
                    await handle_message(msg)
                    # #12: Rate limit между обработкой вакансий
                    await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"Ошибка доступа к каналу {channel}: {e}")
                
        log.info("Исторические вакансии обработаны!")

    log.info("Бот запущен. Мониторим новые вакансии в реальном времени...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram Vacancy Bot")
    parser.add_argument("--mode", choices=["normal", "yest"], default="normal", help="normal = только новые; yest = вчера+сегодня, потом мониторинг")
    args = parser.parse_args()
    
    client.loop.run_until_complete(main(mode=args.mode))
