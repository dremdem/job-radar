"""Store new Gmail messages for each synced query.

``sync_query`` reads the threads matching one ``GmailQuery`` since its cursor, stores
messages not seen yet as ``EmailMessage`` rows and moves the cursor to the newest one;
``sync_gmail`` does it for every query. Re-running is safe: a message is stored once.
See docs/gmail.md.

Usage:
    import job_radar.gmail.sync as gmail_sync
"""

import collections.abc as cabc
import dataclasses
import datetime as dt

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.gmail.client as gmail_client
import job_radar.gmail.parse as gmail_parse
import job_radar.gmail.queries as gmail_queries
import job_radar.models as models

# Re-read a day before the cursor: Gmail's after: is coarse, duplicates are skipped anyway.
CURSOR_OVERLAP = dt.timedelta(days=1)


@dataclasses.dataclass
class SyncStats:
    """Counters of one query's sync: threads read, messages new and already seen."""

    threads: int = 0
    new: int = 0
    seen: int = 0


Progress = cabc.Callable[[str, "SyncStats"], None]


def cursor_name(query: gmail_queries.GmailQuery) -> str:
    return f"gmail:{query.name}"


def _as_utc(value: dt.datetime) -> dt.datetime:
    # SQLite drops tzinfo; everything stored is UTC.
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def build_query(
    query: gmail_queries.GmailQuery,
    *,
    cursor: dt.datetime | None,
    since: dt.date | None,
    now: dt.datetime,
) -> str:
    if since is not None:
        return f"{query.query} after:{since:%Y/%m/%d}"
    if cursor is not None:
        return f"{query.query} after:{int((_as_utc(cursor) - CURSOR_OVERLAP).timestamp())}"
    if query.initial_lookback_days is not None:
        start = now - dt.timedelta(days=query.initial_lookback_days)
        return f"{query.query} after:{int(start.timestamp())}"
    return query.query


def sync_query(
    session: sa_orm.Session,
    client: gmail_client.GmailClient,
    query: gmail_queries.GmailQuery,
    *,
    since: dt.date | None = None,
    now: dt.datetime | None = None,
    progress: Progress | None = None,
) -> SyncStats:
    now = now or dt.datetime.now(dt.UTC)
    cursor = session.get(models.SyncCursor, cursor_name(query)) or models.SyncCursor(
        name=cursor_name(query)
    )
    newest = _as_utc(cursor.last_message_at) if cursor.last_message_at else None
    stats = SyncStats()

    q = build_query(query, cursor=cursor.last_message_at, since=since, now=now)
    for thread_id in client.list_thread_ids(q):
        stats.threads += 1
        for raw in client.get_thread(thread_id).get("messages", []):
            parsed = gmail_parse.parse_message(raw)
            existing = session.scalar(
                sa.select(models.EmailMessage).where(
                    models.EmailMessage.gmail_message_id == parsed.gmail_message_id
                )
            )
            if existing is not None:
                stats.seen += 1
                if query.name not in existing.categories:
                    existing.categories = [*existing.categories, query.name]
            else:
                stats.new += 1
                session.add(
                    models.EmailMessage(
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
    session: sa_orm.Session,
    client: gmail_client.GmailClient,
    *,
    names: cabc.Iterable[str] | None = None,
    since: dt.date | None = None,
    now: dt.datetime | None = None,
    progress: Progress | None = None,
) -> dict[str, SyncStats]:
    selected = (
        [gmail_queries.QUERIES[name] for name in names]
        if names
        else list(gmail_queries.QUERIES.values())
    )
    return {
        q.name: sync_query(session, client, q, since=since, now=now, progress=progress)
        for q in selected
    }
