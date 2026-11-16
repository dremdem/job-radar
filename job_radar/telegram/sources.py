"""The channel allowlist (data/telegram.yaml).

Each ``Source`` is one channel or forum topic with its filters: required tags, keyword
and title patterns, excluded titles. ``load_sources`` reads the file. See
docs/telegram.md and telegram.example.yaml.

Usage:
    import job_radar.telegram.sources as telegram_sources
"""

import dataclasses
import pathlib
import re

import yaml

DEFAULT_KEYWORDS = r"python|django|fastapi|flask"
# The vacancy title must name a developer-type role ...
DEFAULT_TITLE = (
    r"engineer|developer|разработчик|программист|programmer|architect|архитектор|devops|\bsre\b|"
    r"\bml\b|\bai\b|\blead\b|лид|\bcto\b|back-?end|бэкенд|бекенд|python"
)
# ... and not one of these (measured noise in the real channels, see docs/telegram.md).
DEFAULT_EXCLUDE_TITLE = (
    r"analyst|аналитик|manager|менеджер|\bqa\b|\btest|тестиров|recruit|рекрут|designer|дизайн|"
    r"sales|продаж|marketing|маркетолог|support|поддержк|intern\b|стажер|стажёр|дайджест|digest|подборк"
)


@dataclasses.dataclass(frozen=True)
class Source:
    """One allowlisted channel or topic with its filters."""

    name: str
    chat: str | int
    topics: list[int] = dataclasses.field(default_factory=list)
    require_tags: list[str] = dataclasses.field(default_factory=list)
    keywords_pattern: str = DEFAULT_KEYWORDS
    title_pattern: str = DEFAULT_TITLE
    exclude_title_pattern: str = DEFAULT_EXCLUDE_TITLE
    # post | graph_org_digest
    parse: str = "post"
    categories: list[str] = dataclasses.field(default_factory=list)

    @property
    def keywords(self) -> re.Pattern[str]:
        return re.compile(self.keywords_pattern, re.IGNORECASE)

    def wants_title(self, title: str) -> bool:
        return bool(re.search(self.title_pattern, title, re.IGNORECASE)) and not re.search(
            self.exclude_title_pattern, title, re.IGNORECASE
        )


def load_sources(path: pathlib.Path) -> list[Source]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    sources = []
    for raw in data.get("sources") or []:
        chat = raw["chat"]
        if isinstance(chat, str):
            chat = chat.lstrip("@")
        sources.append(
            Source(
                name=str(raw["name"]),
                chat=chat,
                topics=[int(t) for t in raw.get("topics") or []],
                require_tags=[str(t).lower() for t in raw.get("require_tags") or []],
                keywords_pattern=raw.get("keywords") or DEFAULT_KEYWORDS,
                title_pattern=raw.get("title_keywords") or DEFAULT_TITLE,
                exclude_title_pattern=raw.get("exclude_title") or DEFAULT_EXCLUDE_TITLE,
                parse=raw.get("parse") or "post",
                categories=list(raw.get("categories") or []),
            )
        )
    return sources
