"""Events and vacancies added by hand (#182). See docs/queue.md#add-by-hand."""

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import get_or_create_company, upsert_source_record
from .hh.notifications import VACANCY_URL
from .models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    CompanyEvent,
    NoteKind,
    Opportunity,
    OpportunityState,
    RejectionSource,
    SourceRecord,
    SourceType,
)
from .notes import add_note
from .rejections import mark_rejected
from .scoring import score_job

# Kinds you can add; "applied" and "rejection" become an Application and a Rejection.
EVENT_KINDS = {
    "recruiter_outreach": "Recruiter outreach",
    "call": "Call",
    "interview": "Interview",
    "test_task": "Test task",
    "offer": "Offer",
    "applied": "Applied",
    "rejection": "Rejection",
    "other": "Other",
}
MANUAL_STATES = (OpportunityState.NEW, OpportunityState.INTERESTED, OpportunityState.APPLIED)

_LINKEDIN = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d+)")
_HH = re.compile(r"hh\.ru/vacancy/(\d+)")
_LINKEDIN_SOURCES = (
    SourceType.LINKEDIN_ALERT,
    SourceType.LINKEDIN_SEARCH,
    SourceType.LINKEDIN_CAPTURE,
    SourceType.LINKEDIN_LINK,
)


def add_event(
    session: Session, opportunity: Opportunity, kind: str, at: datetime, comment: str = ""
) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"Unknown event kind: {kind}")
    comment = comment.strip() or None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if kind == "applied":
        session.add(
            Application(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                applied_at=at,
                role=opportunity.title,
                source=ApplicationSource.MANUAL,
                review=ApplicationReview.CONFIRMED,
                notes=comment,
            )
        )
        opportunity.state = OpportunityState.APPLIED
        opportunity.state_changed_at = at
        opportunity.auto_ignore_reason = None
    elif kind == "rejection":
        mark_rejected(
            session,
            company_id=opportunity.company_id,
            opportunity_id=opportunity.id,
            source=RejectionSource.MANUAL,
            rejected_at=at,
            role=opportunity.title,
        )
    else:
        session.add(
            CompanyEvent(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                kind=kind,
                at=at,
                comment=comment,
            )
        )
    session.commit()


def delete_event(session: Session, event_id: int) -> CompanyEvent | None:
    event = session.get(CompanyEvent, event_id)
    if event is not None:
        session.delete(event)
        session.commit()
    return event


def source_for_link(link: str) -> tuple[SourceType, str, str] | None:
    """(source type, external id, canonical url) for a pasted link; None for no link."""
    link = link.strip()
    if not link:
        return None
    if match := _LINKEDIN.search(link):
        job_id = match.group(1)
        return SourceType.LINKEDIN_LINK, job_id, f"https://www.linkedin.com/jobs/view/{job_id}/"
    if match := _HH.search(link):
        return SourceType.HH, match.group(1), VACANCY_URL.format(id=match.group(1))
    return SourceType.MANUAL, link[:255], link


def find_by_link(session: Session, link: str) -> Opportunity | None:
    source = source_for_link(link)
    if source is None:
        return None
    kind, external_id, url = source
    kinds = _LINKEDIN_SOURCES if kind is SourceType.LINKEDIN_LINK else (kind,)
    record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type.in_(kinds), SourceRecord.external_id == external_id
        )
    ) or session.scalar(select(SourceRecord).where(SourceRecord.url == url))
    return record.opportunity if record else None


def add_vacancy(
    session: Session,
    *,
    title: str,
    company: str,
    link: str = "",
    location: str = "",
    state: OpportunityState = OpportunityState.NEW,
    comment: str = "",
) -> Opportunity:
    """A vacancy found outside the sources. Raises ValueError for a duplicate link."""
    title, company = title.strip(), company.strip()
    if not title or not company:
        raise ValueError("Title and company are required")
    if state not in MANUAL_STATES:
        raise ValueError(f"State must be one of {', '.join(s.value for s in MANUAL_STATES)}")
    if existing := find_by_link(session, link):
        raise ValueError(f"Already here: #{existing.id} {existing.title}")
    score = score_job(title, location or None)
    opportunity = Opportunity(
        title=title[:255],
        company=get_or_create_company(session, company),
        location=location.strip() or None,
        state=OpportunityState.NEW,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(opportunity)
    session.flush()
    if source := source_for_link(link):
        kind, external_id, url = source
        upsert_source_record(
            session, source_type=kind, external_id=external_id, url=url, opportunity=opportunity
        )
    if state is OpportunityState.APPLIED:
        add_event(session, opportunity, "applied", datetime.now(UTC), comment)
    else:
        opportunity.state = state
        opportunity.state_changed_at = datetime.now(UTC)
        if comment.strip():
            add_note(session, opportunity, NoteKind.NOTE, comment=comment)
    session.commit()
    return opportunity
