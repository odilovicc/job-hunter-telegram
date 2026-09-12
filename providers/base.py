"""Provider abstraction: только discovery + нормализация в Job.

Никакой бизнес-логики (фильтры/скоринг/AI/отправка) здесь быть не должно —
за это отвечает services/job_pipeline.py и applications/.
"""
from abc import ABC, abstractmethod
from typing import List

from models.job import Job


class JobProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_jobs(self) -> List[Job]:
        """Возвращает пачку новых вакансий, уже нормализованных в Job.

        Для poll-based источников (Indeed) вызывается периодически.
        Для event-based источников (Telegram) может использоваться только для
        разовых догрузок истории (--mode=yest) — основной поток идёт через
        нативный Telethon-обработчик, который использует тот же нормализатор
        (см. TelegramJobProvider.to_job).
        """
        raise NotImplementedError
