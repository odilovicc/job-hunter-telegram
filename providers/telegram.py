"""Telegram provider: только discovery + нормализация Telethon-сообщения в Job.

Реалтайм-поток управляется Telethon-событием NewMessage прямо в main.py (это
event-driven источник, а не poll-based) — но и обработчик события, и
fetch_jobs() (используется для --mode=yest) проходят через один и тот же
to_job(), чтобы нормализация не дублировалась в двух местах.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from models.job import Job
from .base import JobProvider


class TelegramJobProvider(JobProvider):
    name = "telegram"

    def __init__(self, client, channels, channel_username_by_chat_id: dict):
        self.client = client
        self.channels = channels
        self._channel_username_by_chat_id = channel_username_by_chat_id

    def to_job(self, message) -> Job:
        """Нормализует Telethon-сообщение в Job. chat_id+message.id уникальны
        только в паре (id сообщений у каждого канала своя последовательность)."""
        channel_username = self._channel_username_by_chat_id.get(message.chat_id)
        text = message.text or ""
        # title намеренно остаётся пустым: вся вакансия — один блок текста, а не
        # title+description отдельно. Если сюда положить первую строку, Job.text
        # будет содержать её дважды — безвредно для фильтров (re.search ищет
        # наличие, не счёт вхождения), но засоряет /stats и отчёты.
        return Job(
            source=self.name,
            external_id=f"{message.chat_id}:{message.id}",
            title=(text.splitlines()[0][:200] if text else ""),
            description=text,
            url="",  # ссылка строится лениво через main.message_link (нужен channel_username_by_chat_id)
            metadata={
                "chat_id": message.chat_id,
                "message_id": message.id,
                "channel_username": channel_username,
            },
        )

    async def fetch_jobs(self, since: Optional[datetime] = None) -> List[Job]:
        """Догрузка истории каналов (используется --mode=yest). since по
        умолчанию — начало вчерашнего дня UTC, как и было в старом коде."""
        if since is None:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            since = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        jobs: List[Job] = []
        for channel in self.channels:
            async for msg in self.client.iter_messages(channel, offset_date=since, reverse=True):
                if msg.text:
                    jobs.append(self.to_job(msg))
        return jobs
