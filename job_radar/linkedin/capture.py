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
    r"(reposted |posted )?\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago|"
    r"\d+\+?\s+(connections?|school alumni|alumni|applicants?)\b.*|over \d+ .*|"
    r"dismiss.*|your profile matches.*|.* with verification)$",
    re.IGNORECASE,
)
_SALARY = re.compile(r"[$€£¥₹]|/\s*(yr|year|hr|hour|mo|month)\b", re.IGNORECASE)
_MAX_TEXT = 20000


_VERIFIED = re.compile(r"\s*\((verified job|with verification)\)\s*$", re.IGNORECASE)


def _lines(text: str) -> list[str]:
    out: list[str] = []
    # "Be an early applicant · 1 day ago" is two noise items on one line.
    for raw in text.splitlines():
        for part in raw.split(" · "):
            # Semantic search repeats the title as "<title> (Verified job)".
            line = _VERIFIED.sub("", part.strip().strip("·").strip())
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
    vacancy: str = (
        ""  # what happened to the open vacancy, e.g. "Acme — Engineer: description saved"
    )

    def as_dict(self) -> dict[str, int | str]:
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
    detail = payload.get("detail") or {}
    detail_id = _job_id(detail.get("job_id"))
    own_card: AlertJob | None = None
    for card in payload.get("cards") or []:
        job_id = _job_id(card.get("job_id"))
        if job_id and job_id == detail_id:
            # The open vacancy is reported on its own line, not as a card (on a vacancy page
            # this "card" is just a link to itself, e.g. its salary); its text may still help.
            own_card = own_card or parse_card_text(job_id, str(card.get("text") or "")[:_MAX_TEXT])
            continue
        stats.cards += 1
        job = parse_card_text(job_id, str(card.get("text") or "")[:_MAX_TEXT]) if job_id else None
        if job is None:
            stats.skipped += 1
        elif add_linkedin_job(session, job, SourceType.LINKEDIN_CAPTURE):
            stats.new += 1
        else:
            stats.known += 1

    if job_id := detail_id:
        record = _record(session, job_id)
        created = False
        title, company = (detail.get("title") or "").strip(), (detail.get("company") or "").strip()
        if not (title and company):
            # The tab title of a job page: "<title> | <company> | LinkedIn".
            parts = [p.strip() for p in str(detail.get("page_title") or "").split(" | ")]
            if len(parts) >= 3 and parts[-1] == "LinkedIn":
                title, company = title or parts[0], company or parts[1]
        location = (detail.get("location") or "").strip() or None
        # Prefer the vacancy's own card (it has the location and Easy Apply), else the tab title.
        from_title = (
            AlertJob(job_id, title, company, location, None, None) if title and company else None
        )
        job = own_card or from_title
        if record is None and job is not None:
            add_linkedin_job(session, job, SourceType.LINKEDIN_CAPTURE)
            stats.new += 1
            created = True
            record = _record(session, job_id)
        description = str(detail.get("description") or "").strip()[:_MAX_TEXT]
        outcome = []
        if created:
            outcome.append("new")
        if record is None:
            outcome.append("not recognised (no title or company on the page)")
        elif not description:
            outcome.append("no description found on the page")
        else:
            _, saved = add_snapshot(session, record, description)
            stats.descriptions += int(saved)
            outcome.append("description saved" if saved else "description already saved")
            if saved:
                from ..auto_ignore import apply_rules  # rules that read the description

                session.refresh(record.opportunity)
                apply_rules(session, record.opportunity)
        if record is not None:
            o = record.opportunity
            title, company = o.title, o.company.name
        label = f"{company} — {title}" if title and company else f"job {job_id}"
        stats.vacancy = f"{label}: {', '.join(outcome)}"
    session.commit()
    return stats
