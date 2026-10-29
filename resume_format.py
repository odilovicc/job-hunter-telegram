"""Readable Telegram rendering of extracted resume facts (display only)."""
import html
import re

BULLET = re.compile(r"^\s*[•●▪■◦·∙*\-–—]\s+")
SECTIONS = ("опыт", "образование", "навыки", "ключевые навыки", "о себе", "обо мне", "контакты",
            "языки", "курсы", "проекты", "достижения", "сертификаты", "желаемая должность",
            "experience", "work experience", "education", "skills", "summary", "about", "contacts",
            "languages", "projects", "certifications", "courses", "achievements")


def is_heading(line):
    text = line.strip().rstrip(":")
    if not text or len(text) > 50 or BULLET.match(line) or text[-1] in ".,;!?":
        return False
    letters = [c for c in text if c.isalpha()]
    return text.casefold() in SECTIONS or (len(letters) >= 3 and all(c.isupper() for c in letters))


def render(facts):
    """Group facts into HTML lines: bold headings, bullets, wrapped lines re-joined."""
    lines = []
    for fact in facts:
        if is_heading(fact):
            lines.append(("h", fact.strip().rstrip(":")))
        elif BULLET.match(fact):
            lines.append(("b", BULLET.sub("", fact).strip()))
        elif lines and lines[-1][0] in "bp" and fact[:1].islower():
            lines[-1] = (lines[-1][0], lines[-1][1] + " " + fact.strip())
        else:
            lines.append(("p", fact.strip()))
    out = []
    for kind, text in lines:
        text = html.escape(text)
        if kind == "h":
            out.append(f"\n<b>{text}</b>")
        else:
            out.append(("• " if kind == "b" else "") + text)
    return "\n".join(out).strip()


def split_messages(text, limit=3800):
    """Split on line boundaries so no line is cut; close and reopen <b> never needed (one line each)."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line: cut at a space
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append((current + "\n" + line[:cut]).strip() if current else line[:cut])
            current, line = "", line[cut:].lstrip()
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current.strip())
            current = ""
        current = f"{current}\n{line}" if current else line
    if current.strip():
        chunks.append(current.strip())
    return chunks
