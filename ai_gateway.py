"""One async queue for every AI caller; no worker thread survives a timeout."""
import asyncio
import logging
import time

log = logging.getLogger("vacancy_bot")
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


class AIGateway:
    def __init__(self, client, config, counter):
        self.provider = config.get("provider", "gemini").strip().lower()
        if self.provider not in ("gemini", "openrouter"):
            raise ValueError("ai.provider должен быть gemini или openrouter")
        self.client = client
        self.config = config
        self.counter = counter
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    @property
    def model(self):
        if self.provider == "openrouter":
            return self.config.get("openrouter_model") or "openrouter/free"
        return self.config.get("model") or DEFAULT_GEMINI_MODEL

    async def generate(self, prompt, system=None, max_output_tokens=700):
        async with self.lock:
            for attempt in range(3):
                delay = float(self.config.get("min_interval_sec", 2)) - (time.monotonic() - self.last_call)
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    options = {"max_output_tokens": max_output_tokens}
                    if system:
                        options["system_instruction"] = system
                    self.counter("ai_requests")
                    if self.provider == "openrouter":
                        request = self.client.generate(self.model, prompt, system, max_output_tokens)
                    else:
                        request = self.client.aio.models.generate_content(
                            model=self.model, contents=prompt, config=options)
                    response = await asyncio.wait_for(
                        request,
                        timeout=float(self.config.get("timeout_sec", 45)),
                    )
                    usage = getattr(response, "usage_metadata", None)
                    for field in ("prompt_token_count", "candidates_token_count", "thoughts_token_count"):
                        count = getattr(usage, field, None)
                        if count:
                            self.counter("ai_" + field, int(count))
                    candidates = getattr(response, "candidates", None)
                    if candidates and str(candidates[0].finish_reason).endswith("MAX_TOKENS"):
                        raise ValueError("Модель не завершила письмо. Увеличьте бюджет ответа модели.")
                    result = (response.text or "").strip()
                    if not result:
                        raise ValueError("ИИ вернул пустой ответ.")
                    return result
                except Exception as exc:
                    # Retry only transient server errors; exhausted quotas remain a saved job.
                    code = getattr(exc, "code", None)
                    if code not in (429, 500, 502, 503, 504) or attempt == 2:
                        raise
                    log.warning("%s временно недоступен: code=%s, попытка=%s", self.provider, code, attempt + 1)
                    retry_after = getattr(exc, "retry_after", None) or 0
                    # Do not hold the shared queue through a daily quota reset.
                    if retry_after > 60:
                        raise
                    await asyncio.sleep(max(2 ** (attempt + 1), retry_after))
                finally:
                    self.last_call = time.monotonic()
