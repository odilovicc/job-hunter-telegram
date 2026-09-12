"""Общий rate-limiter для всех вызовов Gemini (письма для Telegram, structured
analysis для Telegram+Indeed). Раньше троттлинг жил приватно в main.py и
работал только для generate_letter — вынесено сюда, чтобы Indeed-пайплайн не
заводил свой независимый лимитер и не удваивал реальный RPS к одному и тому
же Gemini API key/квоте.
"""
import asyncio
import time


class GeminiThrottle:
    def __init__(self, min_interval_sec: float = 1.0, timeout_sec: float = 30.0):
        self.min_interval_sec = min_interval_sec
        self.timeout_sec = timeout_sec
        # Lock создаётся лениво: до Python 3.10 asyncio.Lock() привязывается к
        # текущему event loop в момент создания, а на импорте модуля нужного
        # loop'а может ещё не быть.
        self._lock = None
        self._last_call = 0.0

    def _get_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run(self, blocking_fn, *args, **kwargs):
        """Выполняет blocking_fn(*args, **kwargs) в отдельном потоке, не чаще
        min_interval_sec между вызовами, с таймаутом timeout_sec. Бросает
        asyncio.TimeoutError при превышении таймаута — вызывающий должен сам
        подставить fallback."""
        async with self._get_lock():
            wait = self.min_interval_sec - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(blocking_fn, *args, **kwargs),
                    timeout=self.timeout_sec,
                )
            finally:
                self._last_call = time.monotonic()


# Единственный на процесс троттлер — импортируйте именно этот инстанс, не
# создавайте новые (иначе они не будут знать друг о друге и лимит удвоится).
gemini_throttle = GeminiThrottle()
