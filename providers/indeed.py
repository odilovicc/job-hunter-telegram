""""Indeed"-провайдер, реализованный через Adzuna API.

Почему не сам Indeed:
Indeed публикует API (Job Sync API, Indeed Apply API — см. docs.indeed.com)
только для РАБОТОДАТЕЛЕЙ/ATS-партнёров: публиковать СВОИ вакансии и получать
отклики на них. Публичного API "искать чужие вакансии как соискатель" у
Indeed нет (Publisher API для новых партнёров закрыт), поэтому прямой честный
путь невозможен, а скрапинг indeed.com в проде мы сознательно не делаем.

Adzuna (https://developer.adzuna.com) — официальный, бесплатный (в рамках
лимитов) агрегатор вакансий с публичным REST API, требующим только app_id +
app_key. Он НЕ является Indeed и не гарантированно включает вакансии именно
с Indeed — это самостоятельный источник, который мы используем как легальную
замену "второго источника вакансий" (см. README.md).

ВАЖНО про страны: Adzuna покрывает ограниченный список стран (gb, us, at, au,
br, ca, de, fr, in, it, mx, nl, nz, pl, ru, sg, za, es, ch, ie — актуальный
список см. https://developer.adzuna.com/overview). Узбекистан и страны
Центральной Азии Adzuna НЕ покрывает — если ваша аудитория именно там,
Indeed-источник имеет смысл настраивать на "remote"-вакансии в поддерживаемых
странах (например country: "gb" + "remote" в search_queries), а не ждать
локальных вакансий.

Формат ответа API (GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}):
{
  "results": [
    {
      "id": "129698749",
      "title": "Frontend Developer",
      "company": {"display_name": "Acme"},
      "location": {"display_name": "London, UK"},
      "description": "...(обрезанное превью)...",
      "redirect_url": "https://www.adzuna.com/details/129698749",
      "salary_min": 40000,
      "salary_max": 55000,
      "contract_type": "permanent",
      "contract_time": "full_time"
    },
    ...
  ]
}
"""
import logging
from typing import List, Optional

import httpx

from models.job import Job
from services.deduplication import fingerprint
from .base import JobProvider

log = logging.getLogger("vacancy_bot")

HTTP_TIMEOUT_SEC = 20
RESULTS_PER_PAGE = 20
ADZUNA_URL_TEMPLATE = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

# Список стран по состоянию на публичную документацию Adzuna. Не используется
# для жёсткой блокировки (Adzuna может добавить страны) — только для
# понятного предупреждения в логе, если код страны выглядит подозрительно.
KNOWN_ADZUNA_COUNTRIES = {
    "gb", "us", "at", "au", "br", "ca", "de", "fr", "in", "it",
    "mx", "nl", "nz", "pl", "ru", "sg", "za", "es", "ch", "ie",
}


class IndeedJobProvider(JobProvider):
    """Имя источника в системе остаётся "indeed" (это второй источник вакансий
    в понимании продукта/UI/статистики) — под капотом запросы идут в Adzuna
    API, см. docstring модуля."""

    name = "indeed"

    def __init__(self, search_queries: List[str], locations: List[str], app_id: str, app_key: str, country: str):
        self.search_queries = search_queries or [""]
        self.locations = locations or [""]
        self.app_id = (app_id or "").strip()
        self.app_key = (app_key or "").strip()
        self.country = (country or "").strip().lower()
        self._warned_no_credentials = False

        if self.country and self.country not in KNOWN_ADZUNA_COUNTRIES:
            log.warning(
                f"sources.indeed.adzuna.country={self.country!r} не входит в известный список "
                f"стран Adzuna ({sorted(KNOWN_ADZUNA_COUNTRIES)}) — если это не опечатка, "
                f"проверьте актуальный список на developer.adzuna.com."
            )

    def _map_item(self, item: dict) -> Optional[Job]:
        title = (item.get("title") or "").strip()
        if not title:
            return None

        company = ((item.get("company") or {}).get("display_name") or "").strip()
        location = ((item.get("location") or {}).get("display_name") or "").strip()

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        salary = ""
        if salary_min or salary_max:
            lo = f"{int(salary_min):,}" if salary_min else "?"
            hi = f"{int(salary_max):,}" if salary_max else "?"
            salary = f"{lo} - {hi}"

        external_id = str(item.get("id") or "").strip()
        if not external_id:
            external_id = fingerprint(company=company, title=title, location=location)

        employment_type = ", ".join(
            filter(None, [item.get("contract_time"), item.get("contract_type")])
        )

        return Job(
            source=self.name,
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            salary=salary,
            description=(item.get("description") or "").strip(),
            url=(item.get("redirect_url") or "").strip(),
            employment_type=employment_type,
            metadata={"raw": item},
        )

    async def fetch_jobs(self) -> List[Job]:
        if not self.app_id or not self.app_key or not self.country:
            if not self._warned_no_credentials:
                log.warning(
                    "sources.indeed включён, но не заданы sources.indeed.adzuna.{app_id,app_key,country} "
                    "в config.yaml — Adzuna-провайдер пропускает опрос. Получить app_id/app_key: "
                    "https://developer.adzuna.com/"
                )
                self._warned_no_credentials = True
            return []

        jobs: List[Job] = []
        url = ADZUNA_URL_TEMPLATE.format(country=self.country)

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http_client:
            for query in self.search_queries:
                for location in self.locations:
                    params = {
                        "app_id": self.app_id,
                        "app_key": self.app_key,
                        "results_per_page": RESULTS_PER_PAGE,
                        "what": query,
                        "content-type": "application/json",
                    }
                    if location:
                        params["where"] = location

                    try:
                        resp = await http_client.get(url, params=params)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        log.error(f"Adzuna: ошибка запроса (what={query!r}, where={location!r}): {e}")
                        continue

                    results = data.get("results") if isinstance(data, dict) else None
                    if not isinstance(results, list):
                        log.error(f"Adzuna: неожиданный формат ответа для what={query!r}, where={location!r}")
                        continue

                    for item in results:
                        if not isinstance(item, dict):
                            continue
                        job = self._map_item(item)
                        if job:
                            jobs.append(job)

        return jobs
