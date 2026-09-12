from dataclasses import dataclass, field
from enum import Enum
from typing import List

from .job import Job
from .candidate import CandidateProfile


class ApplicationStatus(str, Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    DRAFT = "draft"
    READY = "ready"
    APPLIED = "applied"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class ScreenerQuestion:
    """Вопрос анкеты (обычно из Indeed Easy Apply). Заполняется автоматически
    только если ответ железно следует из CandidateProfile — иначе
    requires_confirmation=True и answer оставляем пустым/пометкой."""
    question: str
    answer: str = ""
    confidence: float = 0.0          # 0..1, насколько уверены в answer
    source: str = ""                 # напр. "candidate.skills", "ai:needs_user_input"
    requires_confirmation: bool = True


@dataclass
class ApplicationDraft:
    """Результат ApplicationService.prepare() — всё, что нужно, чтобы показать
    вакансию админу и (если он одобрит) отправить/подать заявку."""
    job: Job
    candidate: CandidateProfile
    cover_letter: str = ""
    resume_name: str = "default"
    resume_path: str = ""
    match_score: int = 0
    recommendation: str = "review"  # apply | review | skip
    strengths: List[str] = field(default_factory=list)
    missing_requirements: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    screener_questions: List[ScreenerQuestion] = field(default_factory=list)
    contact: str = ""  # для Telegram-флоу: username HR, кому шлём резюме
    is_ai_fallback: bool = False


@dataclass
class ApplicationResult:
    success: bool
    status: ApplicationStatus
    message: str = ""
