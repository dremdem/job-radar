"""Read a vacancy out of a Telegram post (one post = one vacancy). See docs/telegram.md."""

import re
from dataclasses import dataclass

_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)\]]+")
_MARKUP = re.compile(r"\*\*|__|~~|`")
# Leading emoji / bullets / symbols before the text of a line.
_LEAD = re.compile(r"^[^\w#(«\"'+]+", re.UNICODE)
# Trailing emoji / symbols after the text of a line ("Developer (AI-Driven) 📈").
_TRAIL = re.compile(r"[^\w)\]»\"'+#.]+$", re.UNICODE)
_HASHTAGS_ONLY = re.compile(r"^(#\w[\w-]*[\s,]*)+$", re.UNICODE)
_NOISE_LINE = re.compile(
    r"^(hiring|we.?re hiring|вакансия|vacancy|ищем|ребята активно ищут\W*)\s*:?\s*$", re.I
)
_LOOKING_FOR = re.compile(r"^(looking for|position|role|позиция|должность)\s*:\s*(.+)$", re.I)
_LOCATION_LINE = re.compile(
    r"^(?:📍|🌐|🌍)\s*(.+)$|^(?:location|локация|location/format)\s*:\s*(.+)$", re.I
)
_COMPANY_LINE = re.compile(r"^(?:company|компания)\s*:\s*(.+)$", re.I)
_TITLE_AT_COMPANY = re.compile(r"^(.+?)\s+(?:в|at|@)\s+(.+)$")
_COMPANY_AND_PLACE = re.compile(
    r"^(.+?),\s*(удал[её]нно|remote|hybrid|гибрид|офис|office|relocation|релокация.*)$", re.I
)


@dataclass(frozen=True)
class PostJob:
    title: str
    company: str | None
    location: str | None
    links: list[str]


def clean(text: str) -> str:
    """Plain text: markdown links reduced to their text, emphasis markers removed."""
    return _MARKUP.sub("", _LINK.sub(r"\1", text))


def _lines(text: str) -> list[str]:
    return [line.strip() for line in clean(text).splitlines() if line.strip()]


def _strip(line: str) -> str:
    return _TRAIL.sub("", _LEAD.sub("", line)).strip(" *|:-—")


def _looks_like_company(name: str) -> bool:
    """A name, not the rest of a sentence ("Компания iGaming в поиске Data Analyst ...")."""
    name = name.strip()
    return bool(name) and len(name.split()) <= 5 and not name[0].islower()


def parse_post(text: str) -> PostJob | None:
    links = [url for _, url in _LINK.findall(text)] + [
        u for u in _BARE_URL.findall(_LINK.sub("", text))
    ]
    lines = _lines(text)
    title = company = location = None

    for line in lines:
        if m := _LOOKING_FOR.match(_strip(line)):
            title = _strip(m.group(2))
            break
    first_block: list[str] = []
    if title is None:
        for i, line in enumerate(lines):
            stripped = _strip(line)
            if not stripped or _HASHTAGS_ONLY.match(stripped) or _NOISE_LINE.match(stripped):
                continue
            title = stripped
            first_block = lines[i + 1 : i + 2]
            break
    if not title:
        return None

    # "Title | Domain" (twinslash), "Title в Company" / "Title at Company" (zarubezhom).
    title = title.split(" | ")[0].strip()
    if (
        (m := _TITLE_AT_COMPANY.match(title))
        and _looks_like_company(m.group(2))
        and len(title) <= 90
    ):
        title, company = m.group(1).strip(), m.group(2).strip()
    # "Title\nCompany, удаленно" (youritjob).
    m = _COMPANY_AND_PLACE.match(_strip(first_block[0])) if first_block else None
    if company is None and m:
        company, location = m.group(1).strip(), m.group(2).strip()

    for line in lines:
        if location is None and (m := _LOCATION_LINE.match(line)):
            location = _strip(m.group(1) or m.group(2)) or None
        if company is None and (m := _COMPANY_LINE.match(_strip(line))):
            company = _strip(m.group(1)) or None
    return PostJob(title=title, company=company, location=location, links=links)
