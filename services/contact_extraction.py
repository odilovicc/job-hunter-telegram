"""Вынесено из main.py, чтобы applications/telegram.py могло переиспользовать
эту логику без циклического импорта (main.py импортирует applications/*)."""
import re


def extract_contact(text: str, channel_usernames: set):
    """Ищет юзернейм HR в тексте вакансии. Возвращает None, если не нашёл.
    channel_usernames — множество юзернеймов каналов (lower, без @), чтобы не
    перепутать канал-источник с реальным HR-контактом."""
    usernames = re.findall(r'@([A-Za-z0-9_]{5,32})', text or "")
    for username in usernames:
        if username.lower() not in channel_usernames:
            return username
    return None
