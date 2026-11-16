"""Vacancies from LinkedIn job alert emails.

``parse_alert`` reads the job cards of one alert email (job id, title, company,
location, salary, Easy Apply); ``canonical_job_url`` drops tracking and personal tokens
from links. ``import_alerts`` creates a vacancy for every job id not seen yet, so re-runs
add nothing twice. See docs/linkedin-alerts.md.

Usage:
    import job_radar.linkedin.alerts as linkedin_alerts
"""

import dataclasses
import re

import bs4
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules

ALERT_SENDER = "jobalerts-noreply@linkedin.com"
_JOB_URL = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_SALARY = re.compile(r"[$€£¥₹]|/\s*(year|yr|month|mo|hour|hr)\b", re.IGNORECASE)
# Email preheaders pad with invisible characters; they come out as "strings" too.
_INVISIBLE = re.compile("^[\\s\\u00a0\\u034f\\u200b-\\u200d\\ufeff]*$")


@dataclasses.dataclass(frozen=True)
class AlertJob:
    """One job card of an alert email."""

    job_id: str
    title: str
    company: str
    location: str | None
    salary: str | None
    # None: the source does not say
    easy_apply: bool | None

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/jobs/view/{self.job_id}/"


def canonical_job_url(url: str) -> str | None:
    """Keep only the job id; everything else in alert links is tracking or personal tokens."""
    match = _JOB_URL.search(url)
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/" if match else None


def _job_anchors(soup: bs4.BeautifulSoup) -> dict[str, list[bs4.Tag]]:
    anchors: dict[str, list[bs4.Tag]] = {}
    for a in soup.find_all("a", href=True):
        if match := _JOB_URL.search(a["href"]):
            anchors.setdefault(match.group(1), []).append(a)
    return anchors


def _card(anchor: bs4.Tag, others: list[str]) -> bs4.Tag:
    """Largest ancestor that still mentions no other job."""
    node = anchor
    while node.parent is not None and not any(f"jobs/view/{o}" in str(node.parent) for o in others):
        node = node.parent
    return node


def _parse_card(job_id: str, anchors: list[bs4.Tag], card: bs4.Tag) -> AlertJob | None:
    texts = [a.get_text(" ", strip=True) for a in anchors]
    title = min((t for t in texts if t), key=len, default="")
    strings = [s for s in card.stripped_strings if not _INVISIBLE.match(s)]
    if not title or title not in strings:
        return None
    rest = strings[strings.index(title) + 1 :]
    if not rest:
        return None
    company, _, location = rest[0].partition(" · ")
    extras = rest[1:]
    return AlertJob(
        job_id=job_id,
        title=title,
        company=company.strip(),
        location=location.strip() or None,
        salary=next((e for e in extras if _SALARY.search(e)), None),
        easy_apply=any(e.lower() == "easy apply" for e in extras),
    )


def parse_alert(html: str) -> list[AlertJob]:
    soup = bs4.BeautifulSoup(html, "html.parser")
    anchors = _job_anchors(soup)
    jobs = []
    for job_id, job_anchors in anchors.items():
        others = [o for o in anchors if o != job_id]
        if job := _parse_card(job_id, job_anchors, _card(job_anchors[0], others)):
            jobs.append(job)
    return jobs


@dataclasses.dataclass
class ImportStats:
    """Counters of one alert import, for the sync status line."""

    emails: int = 0
    jobs: int = 0
    new: int = 0


LINKEDIN_SOURCES = (
    models.SourceType.LINKEDIN_ALERT,
    models.SourceType.LINKEDIN_SEARCH,
    models.SourceType.LINKEDIN_CAPTURE,
    models.SourceType.LINKEDIN_LINK,
)


def add_linkedin_job(
    session: sa_orm.Session,
    job: AlertJob,
    source_type: models.SourceType,
    *,
    remote_known: bool = True,
) -> bool:
    """Create an opportunity for a LinkedIn job id not seen by any LinkedIn source yet."""
    exists = session.scalar(
        sa.select(models.SourceRecord.id).where(
            models.SourceRecord.source_type.in_(LINKEDIN_SOURCES),
            models.SourceRecord.external_id == job.job_id,
        )
    )
    if exists is not None:
        return False
    score = scoring_rules.score_job(job.title, job.location, remote_known=remote_known)
    opportunity = models.Opportunity(
        title=job.title,
        company=dedup.get_or_create_company(session, job.company),
        location=job.location,
        salary=job.salary,
        easy_apply=job.easy_apply,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(
        models.SourceRecord(
            source_type=source_type, external_id=job.job_id, url=job.url, opportunity=opportunity
        )
    )
    session.flush()
    import job_radar.auto_ignore as auto_ignore

    auto_ignore.apply_rules(session, opportunity)
    return True


def import_alerts(session: sa_orm.Session) -> ImportStats:
    """Create opportunities for jobs from stored alert emails. Idempotent by LinkedIn job id."""
    stats = ImportStats()
    emails = session.scalars(
        sa.select(models.EmailMessage).where(
            models.EmailMessage.from_addr.contains(ALERT_SENDER),
            models.EmailMessage.body_html.is_not(None),
        )
    )
    for email in emails:
        stats.emails += 1
        for job in parse_alert(email.body_html or ""):
            stats.jobs += 1
            if add_linkedin_job(session, job, models.SourceType.LINKEDIN_ALERT):
                stats.new += 1
    session.commit()
    return stats
