"""Fresh Python jobs from the Remote OK public JSON feed.

``RemoteOKClient`` reads the feed, ``parse_job`` turns an item into a ``RemoteOKJob``,
``wants`` keeps developer roles that mention Python, and ``import_remoteok`` stores the
new ones. See docs/remoteok.md.

Usage:
    import job_radar.remoteok as remoteok
"""

import dataclasses
import datetime as dt
import json
import re
import typing as t
import urllib.error
import urllib.request

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.http as sources_http
import job_radar.sources.titles as sources_titles

FEED_URL = "https://remoteok.com/api?tag=python"
MAX_AGE = dt.timedelta(days=30)


class RemoteOKError(Exception):
    """The Remote OK feed could not be read."""

    pass


class Client(t.Protocol):
    """What the import needs from Remote OK: the feed items. Tests pass a fake."""

    def feed(self) -> list[dict[str, t.Any]]: ...


class RemoteOKClient:
    """The real client over the public Remote OK JSON feed."""

    def feed(self) -> list[dict[str, t.Any]]:
        request = urllib.request.Request(FEED_URL, headers={"User-Agent": sources_http.USER_AGENT})
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


@dataclasses.dataclass(frozen=True)
class RemoteOKJob:
    """One job from the Remote OK feed, in the shape the import stores."""

    id: str
    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    tags: list[str]
    published_at: dt.datetime


def _fix(text: str) -> str:
    """Repair UTF-8 that was decoded as Latin-1 ("EspaÃ±a" -> "España")."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _salary(raw: dict[str, t.Any]) -> str | None:
    amounts = [int(v) for v in (raw.get("salary_min"), raw.get("salary_max")) if v]
    if not amounts:
        return None
    text = f"{amounts[0]:,}" if len(set(amounts)) == 1 else "-".join(f"{a:,}" for a in amounts)
    return f"{text} USD / year"


def parse_job(raw: dict[str, t.Any]) -> RemoteOKJob:
    place = " ".join(_fix(str(raw.get("location") or "")).split()).strip(" ,")
    return RemoteOKJob(
        id=str(raw["id"]),
        url=str(raw.get("url") or f"https://remoteok.com/remote-jobs/{raw['id']}"),
        title=" ".join(_fix(str(raw.get("position") or "")).split()),
        company=" ".join(_fix(str(raw.get("company") or "")).split()),
        location=f"Remote, {place}" if place else "Remote",
        salary=_salary(raw),
        description=sources_http.html_text(_fix(str(raw.get("description") or ""))),
        tags=[str(tag) for tag in raw.get("tags") or []],
        published_at=dt.datetime.fromtimestamp(int(raw["epoch"]), dt.UTC),
    )


def wants(job: RemoteOKJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(sources_titles.DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(sources_titles.BOARD_EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    text = "\n".join([job.title, " ".join(job.tags), job.description])
    return bool(re.search(sources_titles.DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclasses.dataclass
class RemoteOKStats:
    """Counters of one import, for the sync status line."""

    jobs: int = 0
    old: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    error: str | None = None


def _add(session: sa_orm.Session, job: RemoteOKJob) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

    score = scoring_rules.score_job(job.title, job.location)
    opportunity = models.Opportunity(
        title=job.title[:255],
        company=dedup.get_or_create_company(
            session, job.company or f"Unknown (Remote OK {job.id})"
        ),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.REMOTEOK,
        external_id=job.id,
        url=job.url,
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    if job.description:
        dedup.add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)


def import_remoteok(
    session: sa_orm.Session, client: Client, *, now: dt.datetime | None = None
) -> RemoteOKStats:
    now = now or dt.datetime.now(dt.UTC)
    stats = RemoteOKStats()
    try:
        feed = client.feed()
    except RemoteOKError as e:
        stats.error = str(e)
        return stats
    for raw in feed:
        # the terms-of-use element
        if "id" not in raw or "epoch" not in raw:
            continue
        job = parse_job(raw)
        stats.jobs += 1
        if job.published_at < now - MAX_AGE:
            stats.old += 1
        elif not wants(job):
            stats.skipped += 1
        elif session.scalar(
            sa.select(models.SourceRecord.id).where(
                models.SourceRecord.source_type == models.SourceType.REMOTEOK,
                models.SourceRecord.external_id == job.id,
            )
        ):
            stats.known += 1
        else:
            _add(session, job)
            stats.new += 1
    session.commit()
    return stats
