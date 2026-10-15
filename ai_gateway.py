"""One async queue for every Gemini caller; no worker thread survives a timeout."""
import asyncio
import logging
import time

log = logging.getLogger("vacancy_bot")


class AIGateway:
    def __init__(self, client, config, counter):
        self.client = client
        self.config = config
        self.counter = counter
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    @property
    def model(self):
        return self.config.get("model", "gemini-3.6-flash")

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
                    response = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=self.model, contents=prompt, config=options),
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
                    log.warning("Gemini временно недоступен: code=%s, попытка=%s", code, attempt + 1)
                    await asyncio.sleep(2 ** (attempt + 1))
                finally:
                    self.last_call = time.monotonic()
