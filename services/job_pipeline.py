"""Общий пайплайн, единый для Telegram и Indeed:
Job -> level_1_filter -> level_2_scoring -> dedup -> AI structured analysis
-> выбор резюме -> ScreenerQuestion guard -> ApplicationDraft.

Рендеринг в Telegram UI и отправка/подача заявки — уже разные для каждого
источника (см. applications/), сюда не входят.
"""
import logging
from typing import Dict, List, Optional, Tuple

from filters import level_1_filter, level_2_scoring
from models.job import Job
from models.candidate import CandidateProfile
from models.application import ApplicationDraft, ScreenerQuestion
from ai_generator import generate_structured_analysis, NEEDS_USER_INPUT, FALLBACK_COVER_LETTER
from .deduplication import job_dedup_key
from .gemini_throttle import gemini_throttle

log = logging.getLogger("vacancy_bot")

# Категории вопросов, на которые AI НЕ имеет права отвечать автоматически —
# даже если модель вернула конкретный ответ и высокую confidence, здесь это
# принудительно подавляется в коде (не полагаемся только на промпт).
SENSITIVE_MARKERS = [
    "work authorization", "authorized to work", "authorization to work",
    "visa", "sponsor", "sponsorship",
    "disability", "disabil",
    "gender", "race", "ethnic", "religion", "sexual orientation", "sex ",
    "citizenship",
    "legal right to work",
    # RU
    "виза", "спонсорство", "разрешение на работу", "право на работу",
    "инвалидность", "гражданств", "пол ", "раса", "религ",
]
SALARY_MARKERS = ["salary", "compensation", "зарплат", "оклад"]


def passes_level1(job: Job, config) -> bool:
    return level_1_filter(job.text, config)


def score_job(job: Job, config) -> Tuple[int, bool, list]:
    return level_2_scoring(job.text, config)


def _is_sensitive_question(question: str, candidate: CandidateProfile) -> bool:
    q = (question or "").lower()
    if any(m in q for m in SENSITIVE_MARKERS):
        return True
    if any(m in q for m in SALARY_MARKERS) and not candidate.salary_expectation:
        return True
    return False


def sanitize_screener_questions(raw_questions: List[dict], candidate: CandidateProfile) -> List[ScreenerQuestion]:
    """Пропускает AI-ответы через жёсткий фильтр по чувствительным категориям.

    Это защита "в коде", а не только в промпте: даже если модель
    проигнорировала инструкцию и придумала ответ на "Are you authorized to
    work in the US?", здесь он всё равно будет обнулён и помечен
    requires_confirmation=True.
    """
    result = []
    for raw in raw_questions or []:
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question", "")).strip()
        if not question:
            continue
        answer = str(raw.get("answer", "")).strip()
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if _is_sensitive_question(question, candidate) or answer == NEEDS_USER_INPUT:
            result.append(ScreenerQuestion(
                question=question, answer="", confidence=0.0,
                source="policy:needs_confirmation", requires_confirmation=True,
            ))
            continue

        result.append(ScreenerQuestion(
            question=question,
            answer=answer,
            confidence=confidence,
            source="ai",
            requires_confirmation=bool(raw.get("requires_confirmation", confidence < 0.7)),
        ))
    return result


def pick_resume(recommended_name: Optional[str], resumes: Dict[str, str]) -> Tuple[str, str]:
    """Возвращает (resume_name, resume_path). Падает обратно на 'default',
    если AI порекомендовал ключ, которого нет в конфиге resumes."""
    if recommended_name and recommended_name in resumes:
        return recommended_name, resumes[recommended_name]
    if "default" in resumes:
        return "default", resumes["default"]
    if resumes:
        name, path = next(iter(resumes.items()))
        return name, path
    return "default", ""


async def build_application_draft(job: Job, candidate: CandidateProfile, config, ai_client, resumes: Dict[str, str]) -> ApplicationDraft:
    """Выполняет AI structured analysis (с общим Gemini rate-limiter) и
    собирает ApplicationDraft. Никогда не бросает исключение — при сбое AI
    возвращает консервативный draft с recommendation='review', чтобы вакансия
    не терялась и не отправлялась автоматически как одобренная.
    """
    facts = candidate.known_facts_text()
    resume_names = list(resumes.keys()) or ["default"]

    try:
        analysis = await gemini_throttle.run(
            generate_structured_analysis, job.text, facts, resume_names, config, ai_client
        )
    except Exception as e:
        log.error(f"Сбой AI structured analysis для job {job.id}: {e}")
        analysis = {
            "match_score": 0, "recommendation": "review", "strengths": [],
            "missing_requirements": [], "risks": [f"AI недоступен: {e}"],
            "recommended_resume": "default", "cover_letter": FALLBACK_COVER_LETTER,
            "screener_questions": [], "is_fallback": True,
        }

    resume_name, resume_path = pick_resume(analysis.get("recommended_resume"), resumes)
    screener_questions = sanitize_screener_questions(analysis.get("screener_questions", []), candidate)

    return ApplicationDraft(
        job=job,
        candidate=candidate,
        cover_letter=analysis.get("cover_letter") or FALLBACK_COVER_LETTER,
        resume_name=resume_name,
        resume_path=resume_path,
        match_score=int(analysis.get("match_score") or 0),
        recommendation=analysis.get("recommendation", "review"),
        strengths=list(analysis.get("strengths") or []),
        missing_requirements=list(analysis.get("missing_requirements") or []),
        risks=list(analysis.get("risks") or []),
        screener_questions=screener_questions,
        is_ai_fallback=bool(analysis.get("is_fallback")),
    )


def dedup_and_store(db, job: Job, score: Optional[int] = None, status: str = "discovered"):
    """Сохраняет вакансию в универсальную таблицу jobs (дедуп по source+external_id
    либо fingerprint). Возвращает (job_id, is_new)."""
    key = job_dedup_key(job)
    return db.upsert_job(
        dedup_key=key,
        source=job.source,
        external_id=job.external_id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        description=job.description,
        score=score,
        status=status,
    )
