"""Reasons to ignore a vacancy and free notes; a location reason lowers the score of the
company's other vacancies. See docs/applications.md#reasons-and-notes.
"""

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import NoteKind, Opportunity, OpportunityNote, OpportunityState
from .scoring import Reason

OTHER_COUNTRY_PENALTY = -1.5
SAME_COUNTRY_PENALTY = -3.0
REASON_LABELS = {
    NoteKind.ONLY_LOCATION: "only",
    NoteKind.LANGUAGE: "language",
    NoteKind.STACK: "stack",
    NoteKind.SENIORITY: "seniority",
    NoteKind.SALARY: "salary",
    NoteKind.CONTRACT: "contract",
    NoteKind.OTHER: "other",
}


def country_from_location(location: str | None) -> str | None:
    """Last part of "City, Region, Country", without "(Remote)" and the like."""
    if not location:
        return None
    cleaned = re.sub(r"\((remote|hybrid|on-?site)\)", "", location, flags=re.IGNORECASE)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts[-1] if parts else None


def add_note(
    session: Session,
    opportunity: Opportunity,
    kind: NoteKind,
    detail: str | None = None,
    comment: str | None = None,
) -> OpportunityNote:
    detail = (detail or "").strip() or None
    comment = (comment or "").strip() or None
    if kind is NoteKind.NOTE and not comment:
        raise ValueError("A note needs text")
    if kind is NoteKind.ONLY_LOCATION and not detail:
        raise ValueError("Say which country or region")
    note = OpportunityNote(
        opportunity_id=opportunity.id,
        company_id=opportunity.company_id,
        kind=kind,
        detail=detail if kind in (NoteKind.ONLY_LOCATION, NoteKind.LANGUAGE) else None,
        comment=comment,
    )
    session.add(note)
    session.commit()
    return note


def ignore_with_reason(
    session: Session,
    opportunity: Opportunity,
    kind: NoteKind,
    detail: str | None = None,
    comment: str | None = None,
) -> OpportunityNote:
    if kind is NoteKind.NOTE:
        raise ValueError("Pick a reason")
    opportunity.state = OpportunityState.IGNORE
    return add_note(session, opportunity, kind, detail, comment)


def delete_note(session: Session, note_id: int) -> bool:
    note = session.get(OpportunityNote, note_id)
    if note is None:
        return False
    session.delete(note)
    session.commit()
    return True


def describe(note: OpportunityNote) -> str:
    """History text: "only India · residents only", "salary · too low", "ask about EOR"."""
    if note.kind is NoteKind.NOTE:
        return note.comment or ""
    head = REASON_LABELS[note.kind]
    if note.kind is NoteKind.ONLY_LOCATION:
        head = f"only {note.detail}"
    elif note.kind is NoteKind.LANGUAGE and note.detail:
        head = f"language: {note.detail}"
    return " · ".join(part for part in (head, note.comment) if part)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def location_penalty(session: Session, opportunity: Opportunity) -> tuple[float, Reason] | None:
    """Risk from the latest "only <country>" reason on another vacancy of the same company."""
    note = session.scalar(
        select(OpportunityNote)
        .where(
            OpportunityNote.company_id == opportunity.company_id,
            OpportunityNote.kind == NoteKind.ONLY_LOCATION,
            OpportunityNote.opportunity_id != opportunity.id,
        )
        .order_by(OpportunityNote.created_at.desc(), OpportunityNote.id.desc())
        .limit(1)
    )
    if note is None:
        return None
    here = (country_from_location(opportunity.location) or "").lower()
    points = SAME_COUNTRY_PENALTY if here and here == note.detail.lower() else OTHER_COUNTRY_PENALTY
    company = opportunity.company.name
    text = f"Last time {company} hired only in {note.detail} ({_as_utc(note.created_at):%Y-%m-%d})"
    return points, Reason("-", text)
