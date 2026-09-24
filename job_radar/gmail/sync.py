from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EmailMessage, SyncCursor
from .client import GmailClient
from .parse import parse_message
from .queries import QUERIES, GmailQuery

# Re-read a day before the cursor: Gmail's after: is coarse, duplicates are skipped anyway.
CURSOR_OVERLAP = timedelta(days=1)


@dataclass
class SyncStats:
    threads: int = 0
    new: int = 0
    seen: int = 0


Progress = Callable[[str, "SyncStats"], None]


def cursor_name(query: GmailQuery) -> str:
    return f"gmail:{query.name}"


def _as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo; everything stored is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_query(
    query: GmailQuery, *, cursor: datetime | None, since: date | None, now: datetime
) -> str:
    if since is not None:
        return f"{query.query} after:{since:%Y/%m/%d}"
    if cursor is not None:
        return f"{query.query} after:{int((_as_utc(cursor) - CURSOR_OVERLAP).timestamp())}"
    if query.initial_lookback_days is not None:
        start = now - timedelta(days=query.initial_lookback_days)
        return f"{query.query} after:{int(start.timestamp())}"
    return query.query


def sync_query(
    session: Session,
    client: GmailClient,
    query: GmailQuery,
    *,
    since: date | None = None,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> SyncStats:
    now = now or datetime.now(UTC)
    cursor = session.get(SyncCursor, cursor_name(query)) or SyncCursor(name=cursor_name(query))
    newest = _as_utc(cursor.last_message_at) if cursor.last_message_at else None
    stats = SyncStats()

    q = build_query(query, cursor=cursor.last_message_at, since=since, now=now)
    for thread_id in client.list_thread_ids(q):
        stats.threads += 1
        for raw in client.get_thread(thread_id).get("messages", []):
            parsed = parse_message(raw)
            existing = session.scalar(
                select(EmailMessage).where(EmailMessage.gmail_message_id == parsed.gmail_message_id)
            )
            if existing is not None:
                stats.seen += 1
                if query.name not in existing.categories:
                    existing.categories = [*existing.categories, query.name]
            else:
                stats.new += 1
                session.add(
                    EmailMessage(
                        gmail_message_id=parsed.gmail_message_id,
                        gmail_thread_id=parsed.gmail_thread_id,
                        direction=parsed.direction,
                        sent_at=parsed.sent_at,
                        from_addr=parsed.from_addr,
                        to_addr=parsed.to_addr,
                        subject=parsed.subject,
                        snippet=parsed.snippet,
                        label_ids=parsed.label_ids,
                        categories=[query.name],
                        body_html=parsed.body_html if query.store_body else None,
                        body_text=parsed.body_text if query.store_body else None,
                    )
                )
            if newest is None or parsed.sent_at > newest:
                newest = parsed.sent_at
        # Keep stored messages if a later thread fails; the cursor only moves on success.
        session.commit()
        if progress:
            progress(query.name, stats)

    session.add(cursor)
    cursor.last_message_at = newest
    cursor.last_run_at = now
    session.commit()
    return stats


def sync_gmail(
    session: Session,
    client: GmailClient,
    *,
    names: Iterable[str] | None = None,
    since: date | None = None,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> dict[str, SyncStats]:
    selected = [QUERIES[name] for name in names] if names else list(QUERIES.values())
    return {
        q.name: sync_query(session, client, q, since=since, now=now, progress=progress)
        for q in selected
    }
