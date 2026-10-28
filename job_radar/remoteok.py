"""Fresh Python jobs from the Remote OK public JSON feed. See docs/remoteok.md."""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import add_snapshot, get_or_create_company
from .himalayas import EXCLUDE_TITLE, USER_AGENT, _text
from .models import Opportunity, SourceRecord, SourceType
from .scoring import score_job
from .telegram.sources import DEFAULT_KEYWORDS, DEFAULT_TITLE

FEED_URL = "https://remoteok.com/api?tag=python"
MAX_AGE = timedelta(days=30)


class RemoteOKError(Exception):
    pass


class Client(Protocol):
    def feed(self) -> list[dict[str, Any]]: ...


class RemoteOKClient:
    def feed(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise RemoteOKError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise RemoteOKError(str(e)) from e
        if not isinstance(data, list):
            raise RemoteOKError("unexpected feed format")
        return data


@dataclass(frozen=True)
class RemoteOKJob:
    id: str
    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    tags: list[str]
    published_at: datetime


def _fix(text: str) -> str:
    """Repair UTF-8 that was decoded as Latin-1 ("EspaÃ±a" -> "España")."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _salary(raw: dict[str, Any]) -> str | None:
    amounts = [int(v) for v in (raw.get("salary_min"), raw.get("salary_max")) if v]
    if not amounts:
        return None
    text = f"{amounts[0]:,}" if len(set(amounts)) == 1 else "-".join(f"{a:,}" for a in amounts)
    return f"{text} USD / year"


def parse_job(raw: dict[str, Any]) -> RemoteOKJob:
    place = " ".join(_fix(str(raw.get("location") or "")).split()).strip(" ,")
    return RemoteOKJob(
        id=str(raw["id"]),
        url=str(raw.get("url") or f"https://remoteok.com/remote-jobs/{raw['id']}"),
        title=" ".join(_fix(str(raw.get("position") or "")).split()),
        company=" ".join(_fix(str(raw.get("company") or "")).split()),
        location=f"Remote, {place}" if place else "Remote",
        salary=_salary(raw),
        description=_text(_fix(str(raw.get("description") or ""))),
        tags=[str(t) for t in raw.get("tags") or []],
        published_at=datetime.fromtimestamp(int(raw["epoch"]), UTC),
    )


def wants(job: RemoteOKJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    text = "\n".join([job.title, " ".join(job.tags), job.description])
    return bool(re.search(DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclass
class RemoteOKStats:
    jobs: int = 0
    old: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    error: str | None = None


def _add(session: Session, job: RemoteOKJob) -> None:
    from .auto_ignore import apply_rules

    score = score_job(job.title, job.location)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, job.company or f"Unknown (Remote OK {job.id})"),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=SourceType.REMOTEOK, external_id=job.id, url=job.url, opportunity=opportunity
    )
    session.add(record)
    session.flush()
    if job.description:
        add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_remoteok(
    session: Session, client: Client, *, now: datetime | None = None
) -> RemoteOKStats:
    now = now or datetime.now(UTC)
    stats = RemoteOKStats()
    try:
        feed = client.feed()
    except RemoteOKError as e:
        stats.error = str(e)
        return stats
    for raw in feed:
        if "id" not in raw or "epoch" not in raw:  # the terms-of-use element
            continue
        job = parse_job(raw)
        stats.jobs += 1
        if job.published_at < now - MAX_AGE:
            stats.old += 1
        elif not wants(job):
            stats.skipped += 1
        elif session.scalar(
            select(SourceRecord.id).where(
                SourceRecord.source_type == SourceType.REMOTEOK,
                SourceRecord.external_id == job.id,
            )
        ):
            stats.known += 1
        else:
            _add(session, job)
            stats.new += 1
    session.commit()
    return stats
