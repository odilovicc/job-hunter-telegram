"""Дедупликация вакансий между источниками.

Приоритет: source+external_id (уже гарантирует уникальность внутри одного
источника). Если external_id отсутствует (типично для Indeed-агрегаторов без
стабильного id) — нормализованный fingerprint company+title+location.
"""
import hashlib
import re

from models.job import Job


def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    return s


def fingerprint(company: str, title: str, location: str) -> str:
    """Стабильный ключ для вакансий без external_id. Не криптографический —
    просто чтобы не заводить одинаковые (компания, тайтл, локация) дважды."""
    key = "|".join(_normalize(x) for x in (company, title, location))
    return "fp_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def job_dedup_key(job: Job) -> str:
    """Ключ, по которому job.py считает вакансии одинаковыми в БД (jobs.dedup_key)."""
    if job.external_id and not job.external_id.startswith("fp_"):
        return f"{job.source}:{job.external_id}"
    return f"{job.source}:{fingerprint(job.company, job.title, job.location)}"
