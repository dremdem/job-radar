"""Pages the user sent with the bookmarklet: job cards and, on a vacancy, its description.

The bookmarklet only reads a page the user opened; parsing happens here so it can be tested.
See docs/linkedin-capture.md.
"""

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import add_snapshot
from ..models import SourceRecord, SourceType
from .alerts import LINKEDIN_SOURCES, AlertJob, add_linkedin_job

_NOISE = re.compile(
    r"^(promoted( by hirer)?|viewed|applied|saved|new|top applicant|easy apply|"
    r"be an early applicant|actively (reviewing applicants|recruiting|hiring)|"
    r"responses managed off linkedin|remote|hybrid|on-site|"
    r"(reposted )?\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago|"
    r"\d+\+?\s+(connections?|school alumni|alumni|applicants?)\b.*|over \d+ .*|"
    r"dismiss.*|your profile matches.*|.* with verification)$",
    re.IGNORECASE,
)
_SALARY = re.compile(r"[$€£¥₹]|/\s*(yr|year|hr|hour|mo|month)\b", re.IGNORECASE)
_MAX_TEXT = 20000


def _lines(text: str) -> list[str]:
    out: list[str] = []
    # "Be an early applicant · 1 day ago" is two noise items on one line.
    for raw in text.splitlines():
        for part in raw.split(" · "):
            line = part.strip().strip("·").strip()
            if not line or _NOISE.match(line) or (out and line == out[-1]):
                continue
            out.append(line)
    return out


def parse_card_text(job_id: str, text: str) -> AlertJob | None:
    """Title, company, location, salary and Easy Apply from a search card's visible text."""
    easy_apply = bool(re.search(r"(^|\n|·)\s*easy apply\s*($|\n|·)", text, re.IGNORECASE))
    lines = _lines(text or "")
    salary = next((line for line in lines if _SALARY.search(line)), None)
    lines = [line for line in lines if line != salary]
    if len(lines) < 2:
        return None
    return AlertJob(
        job_id=job_id,
        title=lines[0],
        company=lines[1],
        location=lines[2] if len(lines) > 2 else None,
        salary=salary,
        easy_apply=easy_apply,
    )


@dataclass
class CaptureStats:
    cards: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    descriptions: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def _record(session: Session, job_id: str) -> SourceRecord | None:
    return session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type.in_(LINKEDIN_SOURCES), SourceRecord.external_id == job_id
        )
    )


def _job_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None


def import_capture(session: Session, payload: dict[str, Any]) -> CaptureStats:
    stats = CaptureStats()
    for card in payload.get("cards") or []:
        stats.cards += 1
        job_id = _job_id(card.get("job_id"))
        job = parse_card_text(job_id, str(card.get("text") or "")[:_MAX_TEXT]) if job_id else None
        if job is None:
            stats.skipped += 1
        elif add_linkedin_job(session, job, SourceType.LINKEDIN_CAPTURE):
            stats.new += 1
        else:
            stats.known += 1

    detail = payload.get("detail") or {}
    if job_id := _job_id(detail.get("job_id")):
        record = _record(session, job_id)
        title, company = (detail.get("title") or "").strip(), (detail.get("company") or "").strip()
        if record is None and title and company:
            job = AlertJob(
                job_id, title, company, (detail.get("location") or "").strip() or None, None, None
            )
            add_linkedin_job(session, job, SourceType.LINKEDIN_CAPTURE)
            stats.new += 1
            record = _record(session, job_id)
        description = str(detail.get("description") or "").strip()[:_MAX_TEXT]
        if record is not None and description:
            _, created = add_snapshot(session, record, description)
            stats.descriptions += int(created)
    session.commit()
    return stats
