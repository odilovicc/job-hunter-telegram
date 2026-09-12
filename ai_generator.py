import json
import logging
from google import genai

log = logging.getLogger("vacancy_bot")

FALLBACK_COVER_LETTER = (
    "Здравствуйте! Я изучил вашу вакансию и уверен, что мои навыки отлично подходят. "
    "Мое резюме прикреплено к сообщению. Буду рад обсудить детали."
)

# Поля CandidateProfile, про которые AI НЕ имеет права утверждать что-либо от
# себя — если их нет в профиле, единственный корректный ответ — needs_user_input.
# Дублирует проверку в промпте: это defense-in-depth, а не единственная защита
# (см. services/job_pipeline.py, где ответ ещё раз фильтруется после парсинга).
NEEDS_USER_INPUT = "needs_user_input"

STRUCTURED_ANALYSIS_PROMPT = """Ты — ассистент кандидата, который решает, стоит ли откликаться на вакансию,
и помогает подготовить отклик. Тебе строго ЗАПРЕЩЕНО придумывать факты о кандидате
(опыт, зарплату, образование, сертификаты, work authorization, visa/sponsorship,
уровень языка) — используй ТОЛЬКО то, что указано в профиле кандидата ниже.
Если в профиле кандидата нет данных, необходимых для ответа на какой-то пункт —
верни для него строку "{needs_user_input}", а не предположение.

Профиль кандидата (единственный источник правды о кандидате):
{candidate_facts}

Список доступных резюме (ключ -> файл): {resume_names}

Вакансия:
{job_text}

Верни СТРОГО валидный JSON (без markdown, без ```), со следующими полями:
{{
  "match_score": <0-100 integer, насколько кандидат подходит по фактам из профиля>,
  "recommendation": "apply" | "review" | "skip",
  "strengths": [список коротких строк, чем кандидат подходит],
  "missing_requirements": [список коротких строк, чего в профиле кандидата не хватает под требования вакансии],
  "risks": [список коротких строк с рисками, включая случаи "{needs_user_input}" для критичных данных],
  "recommended_resume": <один из ключей resume_names, наиболее подходящий>,
  "cover_letter": "<короткое сопроводительное письмо 70-100 слов на языке вакансии>",
  "screener_questions": [
    {{"question": "...", "answer": "... или {needs_user_input}", "confidence": 0.0-1.0, "requires_confirmation": true|false}}
  ]
}}
"""

def create_ai_client(api_key, http_options=None):
    """http_options позволяет пустить запросы к Gemini через свой base_url
    (например, реверс-прокси на Cloudflare Worker) — см. network.mode в config.yaml."""
    if http_options is not None:
        return genai.Client(api_key=api_key, http_options=http_options)
    return genai.Client(api_key=api_key)

def generate_cover_letter(vacancy_text, config, ai_client):
    """Генерация сопроводительного письма через Gemini 2.0 Flash."""
    prompt = config["ai"]["prompt_template"].format(vacancy_text=vacancy_text)

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"Ошибка при генерации письма: {e}")
        return FALLBACK_COVER_LETTER


def _fallback_analysis(reason: str) -> dict:
    return {
        "match_score": 0,
        "recommendation": "review",
        "strengths": [],
        "missing_requirements": [],
        "risks": [f"AI-анализ недоступен: {reason}"],
        "recommended_resume": "default",
        "cover_letter": FALLBACK_COVER_LETTER,
        "screener_questions": [],
        "is_fallback": True,
    }


def _extract_json(raw: str) -> str:
    """Gemini иногда оборачивает JSON в ```json ... ``` несмотря на просьбу не делать этого."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


def generate_structured_analysis(job_text: str, candidate_facts: str, resume_names, config, ai_client) -> dict:
    """Возвращает JSON-анализ вакансии (см. STRUCTURED_ANALYSIS_PROMPT). Никогда не
    бросает исключение — при любой ошибке (сеть, невалидный JSON) возвращает
    консервативный fallback с recommendation="review", чтобы вакансия не терялась
    и не отправлялась "как одобренная" молча.
    """
    prompt = STRUCTURED_ANALYSIS_PROMPT.format(
        needs_user_input=NEEDS_USER_INPUT,
        candidate_facts=candidate_facts,
        resume_names=", ".join(resume_names) if resume_names else "default",
        job_text=job_text,
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        raw = _extract_json(response.text)
        data = json.loads(raw)
    except Exception as e:
        log.error(f"Ошибка при структурном AI-анализе вакансии: {e}")
        return _fallback_analysis(str(e))

    if not isinstance(data, dict):
        log.error("Структурный AI-анализ вернул не JSON-объект")
        return _fallback_analysis("невалидный формат ответа")

    data.setdefault("match_score", 0)
    data.setdefault("recommendation", "review")
    data.setdefault("strengths", [])
    data.setdefault("missing_requirements", [])
    data.setdefault("risks", [])
    data.setdefault("recommended_resume", "default")
    data.setdefault("cover_letter", FALLBACK_COVER_LETTER)
    data.setdefault("screener_questions", [])
    data["is_fallback"] = False
    if data.get("recommendation") not in ("apply", "review", "skip"):
        data["recommendation"] = "review"
    return data
