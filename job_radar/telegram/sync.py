"""Store Telegram posts and turn the relevant ones into vacancies. See docs/telegram.md."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..dedup import add_snapshot, get_or_create_company
from ..models import Opportunity, SourceRecord, SourceType, TelegramPost
from ..scoring import score_job
from .posts import clean, parse_post
from .sources import Source

FIRST_RUN_DAYS = 14


@dataclass(frozen=True)
class ReadPlan:
    """What to read from one channel (topic): posts newer than after_id, or since a date."""

    source: Source
    topic_id: int | None
    after_id: int | None
    since: datetime | None


def chat_key(chat: str | int) -> str:
    return str(chat).lstrip("@")


def post_url(chat: str | int, message_id: int) -> str:
    key = chat_key(chat)
    if key.lstrip("-").isdigit():
        # Private channels: t.me/c/<id without the -100 prefix>/<post>.
        return f"https://t.me/c/{re.sub(r'^-100', '', key).lstrip('-')}/{message_id}"
    return f"https://t.me/{key}/{message_id}"


def plan_reads(
    session: Session, sources: list[Source], *, now: datetime, first_run_days: int = FIRST_RUN_DAYS
) -> list[ReadPlan]:
    plans = []
    for source in sources:
        for topic in source.topics or [None]:
            query = select(func.max(TelegramPost.message_id)).where(
                TelegramPost.chat == chat_key(source.chat)
            )
            if topic is not None:
                query = query.where(TelegramPost.topic_id == topic)
            last = session.scalar(query)
            since = None if last else now - timedelta(days=first_run_days)
            plans.append(ReadPlan(source=source, topic_id=topic, after_id=last, since=since))
    return plans


@dataclass
class TelegramStats:
    posts: int = 0
    vacancies: int = 0

    def add(self, other: "TelegramStats") -> None:
        self.posts += other.posts
        self.vacancies += other.vacancies


def _as_utc(value: str | datetime) -> datetime:
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _is_vacancy(source: Source, text: str) -> bool:
    lowered = text.lower()
    if source.require_tags and not any(tag in lowered for tag in source.require_tags):
        return False
    return bool(source.keywords.search(text))


def _create_vacancy(
    session: Session, source: Source, message: dict[str, Any]
) -> Opportunity | None:
    from ..auto_ignore import apply_rules

    job = parse_post(message["text"])
    if job is None or not source.wants_title(job.title):
        return None
    message_id = int(message["id"])
    company_name = job.company or f"Unknown ({source.name} {message_id})"
    score = score_job(job.title, job.location)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, company_name),
        location=job.location,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=SourceType.TELEGRAM,
        external_id=f"{chat_key(source.chat)}:{message_id}",
        url=post_url(source.chat, message_id),
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    add_snapshot(session, record, clean(message["text"]).strip())
    session.refresh(opportunity)
    apply_rules(session, opportunity)
    return opportunity


def import_posts(
    session: Session,
    source: Source,
    topic_id: int | None,
    messages: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> TelegramStats:
    """Store new posts of one channel (topic); create vacancies from the relevant ones."""
    stats = TelegramStats()
    chat = chat_key(source.chat)
    for message in sorted(messages, key=lambda m: int(m["id"])):
        exists = session.scalar(
            select(TelegramPost.id).where(
                TelegramPost.chat == chat, TelegramPost.message_id == int(message["id"])
            )
        )
        if exists is not None:
            continue
        text = message.get("text") or ""
        opportunity = (
            _create_vacancy(session, source, message) if _is_vacancy(source, text) else None
        )
        session.add(
            TelegramPost(
                source=source.name,
                chat=chat,
                topic_id=topic_id,
                message_id=int(message["id"]),
                posted_at=_as_utc(message.get("date") or now or datetime.now(UTC)),
                text=text,
                forwarded_from=message.get("forwarded_from"),
                status="vacancy" if opportunity else "skipped",
                opportunity_id=opportunity.id if opportunity else None,
            )
        )
        stats.posts += 1
        stats.vacancies += int(opportunity is not None)
    session.commit()
    return stats
