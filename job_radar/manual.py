"""Events and vacancies added by hand (#182).

``add_event`` records something that is not in the mail (a call, an interview, an offer)
on a company. ``add_vacancy`` creates a vacancy found outside the sources; a pasted link
is recognized by ``source_for_link`` (LinkedIn, hh.ru and others) so the same vacancy is
not stored twice. See docs/queue.md#add-by-hand.

Usage:
    import job_radar.manual as manual
"""

import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.hh.notifications as hh_notifications
import job_radar.models as models
import job_radar.notes as notes
import job_radar.rejections as rejections
import job_radar.scoring as scoring

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
MANUAL_STATES = (
    models.OpportunityState.NEW,
    models.OpportunityState.INTERESTED,
    models.OpportunityState.APPLIED,
)

_LINKEDIN = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d+)")
_HH = re.compile(r"hh\.ru/vacancy/(\d+)")
_LINKEDIN_SOURCES = (
    models.SourceType.LINKEDIN_ALERT,
    models.SourceType.LINKEDIN_SEARCH,
    models.SourceType.LINKEDIN_CAPTURE,
    models.SourceType.LINKEDIN_LINK,
)


def add_event(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    kind: str,
    at: dt.datetime,
    comment: str = "",
) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"Unknown event kind: {kind}")
    comment = comment.strip() or None
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    if kind == "applied":
        session.add(
            models.Application(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                applied_at=at,
                role=opportunity.title,
                source=models.ApplicationSource.MANUAL,
                review=models.ApplicationReview.CONFIRMED,
                notes=comment,
            )
        )
        opportunity.state = models.OpportunityState.APPLIED
        opportunity.state_changed_at = at
        opportunity.auto_ignore_reason = None
    elif kind == "rejection":
        rejections.mark_rejected(
            session,
            company_id=opportunity.company_id,
            opportunity_id=opportunity.id,
            source=models.RejectionSource.MANUAL,
            rejected_at=at,
            role=opportunity.title,
        )
    else:
        session.add(
            models.CompanyEvent(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                kind=kind,
                at=at,
                comment=comment,
            )
        )
    session.commit()


def source_for_link(link: str) -> tuple[models.SourceType, str, str] | None:
    """(source type, external id, canonical url) for a pasted link; None for no link."""
    link = link.strip()
    if not link:
        return None
    if match := _LINKEDIN.search(link):
        job_id = match.group(1)
        return (
            models.SourceType.LINKEDIN_LINK,
            job_id,
            f"https://www.linkedin.com/jobs/view/{job_id}/",
        )
    if match := _HH.search(link):
        return (
            models.SourceType.HH,
            match.group(1),
            hh_notifications.VACANCY_URL.format(id=match.group(1)),
        )
    return models.SourceType.MANUAL, link[:255], link


def find_by_link(session: sa_orm.Session, link: str) -> models.Opportunity | None:
    source = source_for_link(link)
    if source is None:
        return None
    kind, external_id, url = source
    kinds = _LINKEDIN_SOURCES if kind is models.SourceType.LINKEDIN_LINK else (kind,)
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type.in_(kinds),
            models.SourceRecord.external_id == external_id,
        )
    ) or session.scalar(sa.select(models.SourceRecord).where(models.SourceRecord.url == url))
    return record.opportunity if record else None


def add_vacancy(
    session: sa_orm.Session,
    *,
    title: str,
    company: str,
    link: str = "",
    location: str = "",
    state: models.OpportunityState = models.OpportunityState.NEW,
    comment: str = "",
) -> models.Opportunity:
    """A vacancy found outside the sources. Raises ValueError for a duplicate link."""
    title, company = title.strip(), company.strip()
    if not title or not company:
        raise ValueError("Title and company are required")
    if state not in MANUAL_STATES:
        raise ValueError(f"State must be one of {', '.join(s.value for s in MANUAL_STATES)}")
    if existing := find_by_link(session, link):
        raise ValueError(f"Already here: #{existing.id} {existing.title}")
    score = scoring.score_job(title, location or None)
    opportunity = models.Opportunity(
        title=title[:255],
        company=dedup.get_or_create_company(session, company),
        location=location.strip() or None,
        state=models.OpportunityState.NEW,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(opportunity)
    session.flush()
    if source := source_for_link(link):
        kind, external_id, url = source
        dedup.upsert_source_record(
            session, source_type=kind, external_id=external_id, url=url, opportunity=opportunity
        )
    if state is models.OpportunityState.APPLIED:
        add_event(session, opportunity, "applied", dt.datetime.now(dt.UTC), comment)
    else:
        opportunity.state = state
        opportunity.state_changed_at = dt.datetime.now(dt.UTC)
        if comment.strip():
            notes.add_note(session, opportunity, models.NoteKind.NOTE, comment=comment)
    session.commit()
    return opportunity
