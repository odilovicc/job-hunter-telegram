"""Профиль кандидата — единственный источник правды о том, что мы реально
знаем про кандидата. AI не имеет права утверждать что-либо, чего здесь нет
(см. services/job_pipeline.py::SENSITIVE_QUESTION_MARKERS и ai_generator.py).

Не храните сюда секреты/токены — это профиль, а не credentials. Сам файл
config.yaml, откуда обычно грузится профиль, в .gitignore.
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class CandidateProfile:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    skills: List[str] = field(default_factory=list)
    experience: List[str] = field(default_factory=list)
    education: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    # Пустая строка = "не настроено" -> AI обязан отвечать needs_user_input,
    # а не придумывать цифру.
    salary_expectation: str = ""
    work_authorization: str = ""
    relocation: str = ""

    @classmethod
    def from_config(cls, cfg: dict) -> "CandidateProfile":
        cfg = cfg or {}
        return cls(
            name=cfg.get("name", ""),
            email=cfg.get("email", ""),
            phone=cfg.get("phone", ""),
            location=cfg.get("location", ""),
            skills=list(cfg.get("skills") or []),
            experience=list(cfg.get("experience") or []),
            education=list(cfg.get("education") or []),
            languages=list(cfg.get("languages") or []),
            linkedin=cfg.get("linkedin", ""),
            github=cfg.get("github", ""),
            portfolio=cfg.get("portfolio", ""),
            salary_expectation=cfg.get("salary_expectation", ""),
            work_authorization=cfg.get("work_authorization", ""),
            relocation=cfg.get("relocation", ""),
        )

    def known_facts_text(self) -> str:
        """Компактное текстовое представление для промпта AI — только то, что
        реально заполнено, чтобы модель не путала пустое с "не важно"."""
        lines = []
        for field_name, value in [
            ("Имя", self.name),
            ("Локация", self.location),
            ("Навыки", ", ".join(self.skills)),
            ("Опыт", "; ".join(self.experience)),
            ("Образование", "; ".join(self.education)),
            ("Языки", ", ".join(self.languages)),
            ("Ожидания по зарплате", self.salary_expectation),
            ("Право на работу", self.work_authorization),
            ("Готовность к релокации", self.relocation),
        ]:
            lines.append(f"{field_name}: {value if value else 'НЕИЗВЕСТНО'}")
        return "\n".join(lines)
