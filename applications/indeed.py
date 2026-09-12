from models.job import Job
from models.candidate import CandidateProfile
from models.application import ApplicationDraft, ApplicationResult, ApplicationStatus
from services.job_pipeline import build_application_draft
from .base import ApplicationService


class IndeedApplicationService(ApplicationService):
    """prepare() — AI-анализ + подбор резюме + screener-ответы (только те, что
    железно следуют из CandidateProfile).

    submit() СОЗНАТЕЛЬНО не подаёт заявку сама: Indeed не даёт официального
    API для подачи заявки за кандидата на чужую (не свою) вакансию (см.
    providers/indeed.py). Поэтому submit() лишь помечает черновик как READY
    (готов к ручной подаче на job.url) — реальная подача происходит вручную
    человеком в браузере. Переход в APPLIED делает confirm_applied() ПОСЛЕ
    того, как админ вручную подтвердит в Telegram, что подал заявку.
    """

    def __init__(self, config, ai_client, resumes: dict):
        self.config = config
        self.ai_client = ai_client
        self.resumes = resumes

    async def prepare(self, job: Job, candidate: CandidateProfile) -> ApplicationDraft:
        return await build_application_draft(job, candidate, self.config, self.ai_client, self.resumes)

    async def submit(self, application: ApplicationDraft) -> ApplicationResult:
        if not application.job.url:
            return ApplicationResult(
                success=False, status=ApplicationStatus.FAILED,
                message="У вакансии нет ссылки для подачи заявки.",
            )
        return ApplicationResult(
            success=True, status=ApplicationStatus.READY,
            message=application.job.url,
        )

    @staticmethod
    def confirm_applied() -> ApplicationResult:
        """Вызывается по нажатию «✅ Я откликнулся» в Telegram — единственный
        легитимный способ пометить Indeed-заявку как APPLIED, т.к. сама подача
        происходит вне системы, руками человека."""
        return ApplicationResult(success=True, status=ApplicationStatus.APPLIED, message="Подтверждено вручную.")
