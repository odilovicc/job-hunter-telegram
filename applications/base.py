"""Application abstraction: prepare() готовит черновик (AI-анализ, письмо,
резюме), submit() выполняет ДЕЙСТВИЕ подачи — и это действие принципиально
разное для Telegram (можем отправить резюме сами через Telethon) и Indeed
(официального API для подачи заявки за кандидата нет — см. providers/indeed.py,
поэтому submit() для Indeed никогда не отправляет ничего в сеть от имени
кандидата, только помечает черновик готовым к ручной подаче)."""
from abc import ABC, abstractmethod

from models.job import Job
from models.candidate import CandidateProfile
from models.application import ApplicationDraft, ApplicationResult


class ApplicationService(ABC):
    @abstractmethod
    async def prepare(self, job: Job, candidate: CandidateProfile) -> ApplicationDraft:
        raise NotImplementedError

    @abstractmethod
    async def submit(self, application: ApplicationDraft) -> ApplicationResult:
        raise NotImplementedError
