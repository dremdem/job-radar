"""Store Telegram posts and turn the relevant ones into vacancies.

``plan_reads`` decides what to read from each channel (posts after the last stored one,
or since a date on the first run), and ``import_posts`` stores every new post once as a
``TelegramPost`` and creates vacancies from those that pass the source's filters. See
docs/telegram.md.

Usage:
    import job_radar.telegram.sync as telegram_sync
"""

import dataclasses
import datetime as dt
import re
import typing as t

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring as scoring
import job_radar.telegram.posts as telegram_posts
import job_radar.telegram.sources as telegram_sources

FIRST_RUN_DAYS = 14


@dataclasses.dataclass(frozen=True)
class ReadPlan:
    """What to read from one channel (topic): posts newer than after_id, or since a date."""

    source: telegram_sources.Source
    topic_id: int | None
    after_id: int | None
    since: dt.datetime | None


def chat_key(chat: str | int) -> str:
    return str(chat).lstrip("@")


def post_url(chat: str | int, message_id: int) -> str:
    key = chat_key(chat)
    if key.lstrip("-").isdigit():
        # Private channels: t.me/c/<id without the -100 prefix>/<post>.
        return f"https://t.me/c/{re.sub(r'^-100', '', key).lstrip('-')}/{message_id}"
    return f"https://t.me/{key}/{message_id}"


def plan_reads(
    session: sa_orm.Session,
    sources: list[telegram_sources.Source],
    *,
    now: dt.datetime,
    first_run_days: int = FIRST_RUN_DAYS,
) -> list[ReadPlan]:
    plans = []
    for source in sources:
        for topic in source.topics or [None]:
            query = sa.select(sa.func.max(models.TelegramPost.message_id)).where(
                models.TelegramPost.chat == chat_key(source.chat)
            )
            if topic is not None:
                query = query.where(models.TelegramPost.topic_id == topic)
            last = session.scalar(query)
            since = None if last else now - dt.timedelta(days=first_run_days)
            plans.append(ReadPlan(source=source, topic_id=topic, after_id=last, since=since))
    return plans


@dataclasses.dataclass
class TelegramStats:
    """Counters of one channel read: posts stored and vacancies created."""

    posts: int = 0
    vacancies: int = 0

    def add(self, other: "TelegramStats") -> None:
        self.posts += other.posts
        self.vacancies += other.vacancies


def _as_utc(value: str | dt.datetime) -> dt.datetime:
    when = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _is_vacancy(source: telegram_sources.Source, text: str) -> bool:
    lowered = text.lower()
    if source.require_tags and not any(tag in lowered for tag in source.require_tags):
        return False
    return bool(source.keywords.search(text))


def _create_vacancy(
    session: sa_orm.Session, source: telegram_sources.Source, message: dict[str, t.Any]
) -> models.Opportunity | None:
    import job_radar.auto_ignore as auto_ignore

    job = telegram_posts.parse_post(message["text"])
    if job is None or not source.wants_title(job.title):
        return None
    message_id = int(message["id"])
    company_name = job.company or f"Unknown ({source.name} {message_id})"
    score = scoring.score_job(job.title, job.location)
    opportunity = models.Opportunity(
        title=job.title[:255],
        company=dedup.get_or_create_company(session, company_name),
        location=job.location,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.TELEGRAM,
        external_id=f"{chat_key(source.chat)}:{message_id}",
        url=post_url(source.chat, message_id),
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    dedup.add_snapshot(session, record, telegram_posts.clean(message["text"]).strip())
    session.refresh(opportunity)
    auto_ignore.apply_rules(session, opportunity)
    return opportunity


def import_posts(
    session: sa_orm.Session,
    source: telegram_sources.Source,
    topic_id: int | None,
    messages: list[dict[str, t.Any]],
    *,
    now: dt.datetime | None = None,
) -> TelegramStats:
    """Store new posts of one channel (topic); create vacancies from the relevant ones."""
    stats = TelegramStats()
    chat = chat_key(source.chat)
    for message in sorted(messages, key=lambda m: int(m["id"])):
        exists = session.scalar(
            sa.select(models.TelegramPost.id).where(
                models.TelegramPost.chat == chat,
                models.TelegramPost.message_id == int(message["id"]),
            )
        )
        if exists is not None:
            continue
        text = message.get("text") or ""
        opportunity = (
            _create_vacancy(session, source, message) if _is_vacancy(source, text) else None
        )
        session.add(
            models.TelegramPost(
                source=source.name,
                chat=chat,
                topic_id=topic_id,
                message_id=int(message["id"]),
                posted_at=_as_utc(message.get("date") or now or dt.datetime.now(dt.UTC)),
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
