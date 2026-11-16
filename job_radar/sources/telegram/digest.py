"""Digital nomads digests: categories on graph.org, vacancies on telegra.ph.

``digest_links`` picks the chosen categories of a digest post, ``parse_category`` reads
the vacancy list of a category page and ``parse_vacancy`` one vacancy page.
``TelegraphClient`` reads the public Telegraph API with pauses and no login;
``import_digest`` creates vacancies from it. See docs/telegram.md#digests-digital-nomads.

Usage:
    import job_radar.sources.telegram.digest as telegram_digest
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import json
import re
import time
import typing as t
import urllib.error
import urllib.request

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.linkedin.alerts as linkedin_alerts
import job_radar.sources.telegram.sources as telegram_sources
import job_radar.sources.telegram.sync as telegram_sync

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


def _text(node: t.Any) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.get("children", []))


def _links(node: t.Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    own = [node.get("attrs", {}).get("href")] if node.get("tag") == "a" else []
    return [h for h in own if h] + [h for child in node.get("children", []) for h in _links(child)]


@dataclasses.dataclass(frozen=True)
class DigestItem:
    """One vacancy listed on a digest category page."""

    # where the channel found it: HeadHunter, LinkedIn, ...
    group: str
    title: str
    company: str
    # telegra.ph page of the vacancy
    path: str

    @property
    def url(self) -> str:
        return f"https://telegra.ph/{self.path}"


def parse_category(page: dict[str, t.Any]) -> list[DigestItem]:
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


@dataclasses.dataclass(frozen=True)
class VacancyPage:
    """A vacancy page on telegra.ph: its description and the original link."""

    description: str
    original_url: str | None


def parse_vacancy(page: dict[str, t.Any]) -> VacancyPage:
    parts = [
        _text(n).strip()
        for n in page.get("content", [])
        if isinstance(n, dict) and n.get("tag") != "a"
    ]
    links = [h for n in page.get("content", []) for h in _links(n) if "telegra.ph" not in h]
    return VacancyPage("\n\n".join(p for p in parts if p), links[-1] if links else None)


class TelegraphClient:
    """Public Telegraph API, paced; no login."""

    def __init__(self, delay: float = 0.3, sleep: cabc.Callable[[float], None] = time.sleep):
        self._delay, self._sleep, self._called = delay, sleep, False

    def get_page(self, path: str) -> dict[str, t.Any] | None:
        if self._called:
            self._sleep(self._delay)
        self._called = True
        try:
            with urllib.request.urlopen(TELEGRAPH_API.format(path=path), timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError):
            return None
        return data.get("result") if data.get("ok") else None


@dataclasses.dataclass
class DigestStats:
    """Counters of one digest import, for the sync status line."""

    posts: int = 0
    vacancies: int = 0
    # matched a vacancy already known from LinkedIn
    linked: int = 0
    errors: int = 0


def _record(session: sa_orm.Session, types, external_id: str) -> models.SourceRecord | None:
    return session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type.in_(types),
            models.SourceRecord.external_id == external_id,
        )
    )


def _add_vacancy(
    session: sa_orm.Session,
    source: telegram_sources.Source,
    item: DigestItem,
    page: VacancyPage,
    stats: DigestStats,
) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

    key = f"telegraph:{item.path}"
    if _record(session, [models.SourceType.TELEGRAM], key):
        return
    match = _LINKEDIN_JOB.search(page.original_url or "")
    job_id = match.group(1) if match else None
    known = _record(session, linkedin_alerts.LINKEDIN_SOURCES, job_id) if job_id else None
    if known is not None:
        # Same job as one from LinkedIn: give it the description, don't create a twin.
        record = models.SourceRecord(
            source_type=models.SourceType.TELEGRAM,
            external_id=key,
            url=item.url,
            opportunity=known.opportunity,
        )
        session.add(record)
        session.flush()
        dedup.add_snapshot(session, record, page.description)
        session.refresh(known.opportunity)
        review_auto_ignore.apply_rules(session, known.opportunity)
        stats.linked += 1
        return
    text = f"{item.title}\n{page.description}"
    if not (source.wants_title(item.title) and source.keywords.search(text)):
        return
    score = scoring_rules.score_job(item.title, "Remote")
    opportunity = models.Opportunity(
        title=item.title[:255],
        company=dedup.get_or_create_company(
            session, item.company or f"Unknown ({source.name} {item.path})"
        ),
        location="Remote",
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.TELEGRAM,
        external_id=key,
        url=item.url,
        opportunity=opportunity,
    )
    session.add(record)
    if job_id:
        session.add(
            models.SourceRecord(
                source_type=models.SourceType.LINKEDIN_LINK,
                external_id=job_id,
                url=linkedin_alerts.canonical_job_url(page.original_url),
                opportunity=opportunity,
            )
        )
    session.flush()
    dedup.add_snapshot(session, record, page.description)
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)
    stats.vacancies += 1


def import_digest(
    session: sa_orm.Session,
    source: telegram_sources.Source,
    messages: list[dict[str, t.Any]],
    client: TelegraphClient,
    *,
    now: dt.datetime | None = None,
) -> DigestStats:
    stats = DigestStats()
    chat = telegram_sync.chat_key(source.chat)
    budget = MAX_VACANCY_PAGES
    for message in sorted(messages, key=lambda m: int(m["id"])):
        if session.scalar(
            sa.select(models.TelegramPost.id).where(
                models.TelegramPost.chat == chat,
                models.TelegramPost.message_id == int(message["id"]),
            )
        ):
            continue
        for _, path in digest_links(message.get("text") or "", source.categories):
            category = client.get_page(path)
            if category is None:
                stats.errors += 1
                continue
            for item in parse_category(category):
                if budget <= 0 or _record(
                    session, [models.SourceType.TELEGRAM], f"telegraph:{item.path}"
                ):
                    continue
                budget -= 1
                page = client.get_page(item.path)
                if page is None:
                    stats.errors += 1
                    continue
                _add_vacancy(session, source, item, parse_vacancy(page), stats)
        session.add(
            models.TelegramPost(
                source=source.name,
                chat=chat,
                topic_id=None,
                message_id=int(message["id"]),
                posted_at=telegram_sync._as_utc(
                    message.get("date") or now or dt.datetime.now(dt.UTC)
                ),
                text=message.get("text") or "",
                status="digest",
            )
        )
        stats.posts += 1
        session.commit()
    return stats
