"""Reasons to ignore a vacancy, and free notes.

A reason has a kind (location, language, stack, salary and so on), a detail and a
comment; ``ignore_with_reason`` stores it and moves the vacancy to Ignore. A location
reason ("only India") also lowers the score of the company's other vacancies through
``location_penalty``. ``describe`` gives the short text shown in the history. See
docs/applications.md#reasons-and-notes.

Usage:
    import job_radar.notes as notes
"""

import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models
import job_radar.scoring as scoring

OTHER_COUNTRY_PENALTY = -1.5
SAME_COUNTRY_PENALTY = -3.0
REASON_LABELS = {
    models.NoteKind.ONLY_LOCATION: "only",
    models.NoteKind.LANGUAGE: "language",
    models.NoteKind.STACK: "stack",
    models.NoteKind.SENIORITY: "seniority",
    models.NoteKind.SALARY: "salary",
    models.NoteKind.CONTRACT: "contract",
    models.NoteKind.WORK_FORMAT: "hybrid / on-site",
    models.NoteKind.OTHER: "other",
}
# A picture per reason, so a list of ignores reads at a glance (#195).
REASON_ICONS = {
    # airplane: you would have to move
    models.NoteKind.ONLY_LOCATION: "\u2708\ufe0f",
    # speaking head
    models.NoteKind.LANGUAGE: "\U0001f5e3\ufe0f",
    # toolbox
    models.NoteKind.STACK: "\U0001f9f0",
    # office building
    models.NoteKind.WORK_FORMAT: "\U0001f3e2",
    # graduation cap
    models.NoteKind.SENIORITY: "\U0001f393",
    # money bag
    models.NoteKind.SALARY: "\U0001f4b0",
    # memo
    models.NoteKind.CONTRACT: "\U0001f4dd",
    # shrug
    models.NoteKind.OTHER: "\U0001f937",
    # spiral notepad
    models.NoteKind.NOTE: "\U0001f5d2\ufe0f",
}


def reason_icon(kind: models.NoteKind | str) -> str:
    try:
        return REASON_ICONS[models.NoteKind(kind)]
    except ValueError:
        return ""


def country_from_location(location: str | None) -> str | None:
    """Last part of "City, Region, Country", without "(Remote)" and the like."""
    if not location:
        return None
    cleaned = re.sub(r"\((remote|hybrid|on-?site)\)", "", location, flags=re.IGNORECASE)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts[-1] if parts else None


def add_note(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    kind: models.NoteKind,
    detail: str | None = None,
    comment: str | None = None,
) -> models.OpportunityNote:
    detail = (detail or "").strip() or None
    comment = (comment or "").strip() or None
    if kind is models.NoteKind.NOTE and not comment:
        raise ValueError("A note needs text")
    if kind is models.NoteKind.ONLY_LOCATION and not detail:
        raise ValueError("Say which country or region")
    note = models.OpportunityNote(
        opportunity_id=opportunity.id,
        company_id=opportunity.company_id,
        kind=kind,
        detail=detail
        if kind in (models.NoteKind.ONLY_LOCATION, models.NoteKind.LANGUAGE, models.NoteKind.STACK)
        else None,
        comment=comment,
    )
    session.add(note)
    session.commit()
    return note


def ignore_with_reason(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    kind: models.NoteKind,
    detail: str | None = None,
    comment: str | None = None,
) -> models.OpportunityNote:
    if kind is models.NoteKind.NOTE:
        raise ValueError("Pick a reason")
    opportunity.state = models.OpportunityState.IGNORE
    opportunity.state_changed_at = dt.datetime.now(dt.UTC)
    return add_note(session, opportunity, kind, detail, comment)


def delete_note(session: sa_orm.Session, note_id: int) -> bool:
    note = session.get(models.OpportunityNote, note_id)
    if note is None:
        return False
    session.delete(note)
    session.commit()
    return True


def describe(note: models.OpportunityNote) -> str:
    """History text: "only India · residents only", "salary · too low", "ask about EOR"."""
    if note.kind is models.NoteKind.NOTE:
        return note.comment or ""
    head = REASON_LABELS[note.kind]
    if note.kind is models.NoteKind.ONLY_LOCATION:
        head = f"only {note.detail}"
    elif note.kind in (models.NoteKind.LANGUAGE, models.NoteKind.STACK) and note.detail:
        head = f"{REASON_LABELS[note.kind]}: {note.detail}"
    return " · ".join(part for part in (head, note.comment) if part)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def location_penalty(
    session: sa_orm.Session, opportunity: models.Opportunity
) -> tuple[float, scoring.Reason] | None:
    """Risk from the latest "only <country>" reason on another vacancy of the same company."""
    note = session.scalar(
        sa.select(models.OpportunityNote)
        .where(
            models.OpportunityNote.company_id == opportunity.company_id,
            models.OpportunityNote.kind == models.NoteKind.ONLY_LOCATION,
            models.OpportunityNote.opportunity_id != opportunity.id,
        )
        .order_by(models.OpportunityNote.created_at.desc(), models.OpportunityNote.id.desc())
        .limit(1)
    )
    if note is None:
        return None
    here = (country_from_location(opportunity.location) or "").lower()
    points = SAME_COUNTRY_PENALTY if here and here == note.detail.lower() else OTHER_COUNTRY_PENALTY
    company = opportunity.company.name
    text = f"Last time {company} hired only in {note.detail} ({_as_utc(note.created_at):%Y-%m-%d})"
    return points, scoring.Reason("-", text)
