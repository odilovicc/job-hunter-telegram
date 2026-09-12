import asyncio

from models.job import Job
from models.candidate import CandidateProfile
from models.application import ApplicationDraft, ApplicationResult, ApplicationStatus
from services.contact_extraction import extract_contact
from services.job_pipeline import build_application_draft
from .base import ApplicationService


class TelegramApplicationService(ApplicationService):
    """prepare() — AI-анализ + подбор резюме + поиск HR-контакта в тексте.
    submit() — отправка резюме+письма контакту через Telethon (юзер-сессия;
    обычный Bot API не может писать первым людям, которые не начинали диалог
    с ботом — это касается почти всех HR)."""

    def __init__(self, telethon_client, channel_usernames: set, resumes: dict, config, ai_client):
        self.client = telethon_client
        self.channel_usernames = channel_usernames
        self.resumes = resumes
        self.config = config
        self.ai_client = ai_client

    async def prepare(self, job: Job, candidate: CandidateProfile) -> ApplicationDraft:
        draft = await build_application_draft(job, candidate, self.config, self.ai_client, self.resumes)
        draft.contact = extract_contact(job.description, self.channel_usernames) or ""
        return draft

    async def submit(self, application: ApplicationDraft) -> ApplicationResult:
        if not application.contact:
            return ApplicationResult(
                success=False, status=ApplicationStatus.FAILED,
                message="Контакт HR не найден в тексте вакансии — отправлять некому.",
            )
        try:
            async with self.client.action(application.contact, 'typing'):
                await asyncio.sleep(2)
            await self.client.send_file(
                application.contact,
                file=application.resume_path,
                caption=application.cover_letter,
            )
        except Exception as e:
            return ApplicationResult(success=False, status=ApplicationStatus.FAILED, message=str(e))

        return ApplicationResult(
            success=True, status=ApplicationStatus.APPLIED,
            message=f"Отправлено @{application.contact}",
        )
