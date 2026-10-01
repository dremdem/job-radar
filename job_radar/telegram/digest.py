"""Digital nomads digests: categories on graph.org, vacancies on telegra.ph. See
docs/telegram.md#digests-digital-nomads."""

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import add_snapshot, get_or_create_company
from ..linkedin.alerts import LINKEDIN_SOURCES, canonical_job_url
from ..models import Opportunity, SourceRecord, SourceType, TelegramPost
from ..scoring import score_job
from .sources import Source
from .sync import _as_utc, chat_key

TELEGRAPH_API = "https://api.telegra.ph/getPage/{path}?return_content=true"
MAX_VACANCY_PAGES = 300
_CATEGORY_LINK = re.compile(r"\[([^\]]+?)\s*-\s*\d+\s*\]\((https://graph\.org/([^)\s]+))\)")
_LINKEDIN_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d+)")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def digest_links(text: str, categories: list[str]) -> list[tuple[str, str]]:
    """(category, graph.org path) for the chosen categories of a digest post."""
    wanted = {_norm(c) for c in categories}
    return [
        (name.strip(), path)
        for name, _, path in _CATEGORY_LINK.findall(text)
        if _norm(name) in wanted
    ]


def _text(node: Any) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.get("children", []))


def _links(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    own = [node.get("attrs", {}).get("href")] if node.get("tag") == "a" else []
    return [h for h in own if h] + [h for child in node.get("children", []) for h in _links(child)]


@dataclass(frozen=True)
class DigestItem:
    group: str  # where the channel found it: HeadHunter, LinkedIn, ...
    title: str
    company: str
    path: str  # telegra.ph page of the vacancy

    @property
    def url(self) -> str:
        return f"https://telegra.ph/{self.path}"


def parse_category(page: dict[str, Any]) -> list[DigestItem]:
    items, group = [], ""
    for node in page.get("content", []):
        if not isinstance(node, dict):
            continue
        if node.get("tag") in ("h3", "h4"):
            group = _text(node).strip()
            continue
        links = [h for h in _links(node) if "telegra.ph/" in h]
        if not links:
            continue
        title, _, company = _text(node).strip().rpartition(" at ")
        if not title:
            title, company = company, ""
        items.append(
            DigestItem(
                group, title.strip(), company.strip(), links[0].rstrip("/").rsplit("/", 1)[1]
            )
        )
    return items


@dataclass(frozen=True)
class VacancyPage:
    description: str
    original_url: str | None


def parse_vacancy(page: dict[str, Any]) -> VacancyPage:
    parts = [
        _text(n).strip()
        for n in page.get("content", [])
        if isinstance(n, dict) and n.get("tag") != "a"
    ]
    links = [h for n in page.get("content", []) for h in _links(n) if "telegra.ph" not in h]
    return VacancyPage("\n\n".join(p for p in parts if p), links[-1] if links else None)


class TelegraphClient:
    """Public Telegraph API, paced; no login."""

    def __init__(self, delay: float = 0.3, sleep: Callable[[float], None] = time.sleep):
        self._delay, self._sleep, self._called = delay, sleep, False

    def get_page(self, path: str) -> dict[str, Any] | None:
        if self._called:
            self._sleep(self._delay)
        self._called = True
        try:
            with urllib.request.urlopen(TELEGRAPH_API.format(path=path), timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError):
            return None
        return data.get("result") if data.get("ok") else None


@dataclass
class DigestStats:
    posts: int = 0
    vacancies: int = 0
    linked: int = 0  # matched a vacancy already known from LinkedIn
    errors: int = 0


def _record(session: Session, types, external_id: str) -> SourceRecord | None:
    return session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type.in_(types), SourceRecord.external_id == external_id
        )
    )


def _add_vacancy(
    session: Session, source: Source, item: DigestItem, page: VacancyPage, stats: DigestStats
) -> None:
    from ..auto_ignore import apply_rules

    key = f"telegraph:{item.path}"
    if _record(session, [SourceType.TELEGRAM], key):
        return
    match = _LINKEDIN_JOB.search(page.original_url or "")
    job_id = match.group(1) if match else None
    known = _record(session, LINKEDIN_SOURCES, job_id) if job_id else None
    if known is not None:
        # Same job as one from LinkedIn: give it the description, don't create a twin.
        record = SourceRecord(
            source_type=SourceType.TELEGRAM,
            external_id=key,
            url=item.url,
            opportunity=known.opportunity,
        )
        session.add(record)
        session.flush()
        add_snapshot(session, record, page.description)
        session.refresh(known.opportunity)
        apply_rules(session, known.opportunity)
        stats.linked += 1
        return
    text = f"{item.title}\n{page.description}"
    if not (source.wants_title(item.title) and source.keywords.search(text)):
        return
    score = score_job(item.title, "Remote")
    opportunity = Opportunity(
        title=item.title[:255],
        company=get_or_create_company(
            session, item.company or f"Unknown ({source.name} {item.path})"
        ),
        location="Remote",
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=SourceType.TELEGRAM, external_id=key, url=item.url, opportunity=opportunity
    )
    session.add(record)
    if job_id:
        session.add(
            SourceRecord(
                source_type=SourceType.LINKEDIN_LINK,
                external_id=job_id,
                url=canonical_job_url(page.original_url),
                opportunity=opportunity,
            )
        )
    session.flush()
    add_snapshot(session, record, page.description)
    session.refresh(opportunity)
    apply_rules(session, opportunity)
    stats.vacancies += 1


def import_digest(
    session: Session,
    source: Source,
    messages: list[dict[str, Any]],
    client: TelegraphClient,
    *,
    now: datetime | None = None,
) -> DigestStats:
    stats = DigestStats()
    chat = chat_key(source.chat)
    budget = MAX_VACANCY_PAGES
    for message in sorted(messages, key=lambda m: int(m["id"])):
        if session.scalar(
            select(TelegramPost.id).where(
                TelegramPost.chat == chat, TelegramPost.message_id == int(message["id"])
            )
        ):
            continue
        for _, path in digest_links(message.get("text") or "", source.categories):
            category = client.get_page(path)
            if category is None:
                stats.errors += 1
                continue
            for item in parse_category(category):
                if budget <= 0 or _record(session, [SourceType.TELEGRAM], f"telegraph:{item.path}"):
                    continue
                budget -= 1
                page = client.get_page(item.path)
                if page is None:
                    stats.errors += 1
                    continue
                _add_vacancy(session, source, item, parse_vacancy(page), stats)
        session.add(
            TelegramPost(
                source=source.name,
                chat=chat,
                topic_id=None,
                message_id=int(message["id"]),
                posted_at=_as_utc(message.get("date") or now or datetime.now(UTC)),
                text=message.get("text") or "",
                status="digest",
            )
        )
        stats.posts += 1
        session.commit()
    return stats
