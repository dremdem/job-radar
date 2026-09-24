import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import get_or_create_company
from ..models import EmailMessage, Opportunity, SourceRecord, SourceType
from ..scoring import score_job

ALERT_SENDER = "jobalerts-noreply@linkedin.com"
_JOB_URL = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_SALARY = re.compile(r"[$€£¥₹]|/\s*(year|yr|month|mo|hour|hr)\b", re.IGNORECASE)
# Email preheaders pad with invisible characters; they come out as "strings" too.
_INVISIBLE = re.compile("^[\\s\\u00a0\\u034f\\u200b-\\u200d\\ufeff]*$")


@dataclass(frozen=True)
class AlertJob:
    job_id: str
    title: str
    company: str
    location: str | None
    salary: str | None
    easy_apply: bool

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/jobs/view/{self.job_id}/"


def canonical_job_url(url: str) -> str | None:
    """Keep only the job id; everything else in alert links is tracking or personal tokens."""
    match = _JOB_URL.search(url)
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/" if match else None


def _job_anchors(soup: BeautifulSoup) -> dict[str, list[Tag]]:
    anchors: dict[str, list[Tag]] = {}
    for a in soup.find_all("a", href=True):
        if match := _JOB_URL.search(a["href"]):
            anchors.setdefault(match.group(1), []).append(a)
    return anchors


def _card(anchor: Tag, others: list[str]) -> Tag:
    """Largest ancestor that still mentions no other job."""
    node = anchor
    while node.parent is not None and not any(f"jobs/view/{o}" in str(node.parent) for o in others):
        node = node.parent
    return node


def _parse_card(job_id: str, anchors: list[Tag], card: Tag) -> AlertJob | None:
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
    soup = BeautifulSoup(html, "html.parser")
    anchors = _job_anchors(soup)
    jobs = []
    for job_id, job_anchors in anchors.items():
        others = [o for o in anchors if o != job_id]
        if job := _parse_card(job_id, job_anchors, _card(job_anchors[0], others)):
            jobs.append(job)
    return jobs


@dataclass
class ImportStats:
    emails: int = 0
    jobs: int = 0
    new: int = 0


def import_alerts(session: Session) -> ImportStats:
    """Create opportunities for jobs from stored alert emails. Idempotent by LinkedIn job id."""
    stats = ImportStats()
    emails = session.scalars(
        select(EmailMessage).where(
            EmailMessage.from_addr.contains(ALERT_SENDER), EmailMessage.body_html.is_not(None)
        )
    )
    for email in emails:
        stats.emails += 1
        for job in parse_alert(email.body_html or ""):
            stats.jobs += 1
            exists = session.scalar(
                select(SourceRecord.id).where(
                    SourceRecord.source_type == SourceType.LINKEDIN_ALERT,
                    SourceRecord.external_id == job.job_id,
                )
            )
            if exists is not None:
                continue
            score = score_job(job.title, job.location)
            opportunity = Opportunity(
                title=job.title,
                company=get_or_create_company(session, job.company),
                location=job.location,
                salary=job.salary,
                easy_apply=job.easy_apply,
                score=score.value,
                score_reasons=[r.as_dict() for r in score.reasons],
            )
            session.add(
                SourceRecord(
                    source_type=SourceType.LINKEDIN_ALERT,
                    external_id=job.job_id,
                    url=job.url,
                    opportunity=opportunity,
                )
            )
            session.flush()
            stats.new += 1
    session.commit()
    return stats
