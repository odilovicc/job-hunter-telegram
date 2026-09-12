"""Единая модель вакансии, общая для всех источников (Telegram, Indeed, ...).

Провайдеры (см. providers/) обязаны нормализовать всё, что они получают, в
этот dataclass — дальше по пайплайну (фильтры, скоринг, AI, БД, Telegram UI)
работают только с Job и никогда не заглядывают обратно в исходный
Telegram-message/HTTP-ответ.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict


@dataclass
class Job:
    source: str            # "telegram" | "indeed" | ...
    external_id: str        # id вакансии в рамках source (напр. "chat_id:message_id" или id из Indeed-фида)
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    description: str = ""
    url: str = ""
    employment_type: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = ""  # глобально уникальный ключ, вычисляется автоматически

    def __post_init__(self):
        if not self.id:
            self.id = f"{self.source}:{self.external_id}"

    @property
    def text(self) -> str:
        """Текст, по которому работают level_1_filter/level_2_scoring — для
        Telegram это message.text целиком, для Indeed — заголовок+описание."""
        parts = [self.title, self.company, self.description]
        return "\n".join(p for p in parts if p)
