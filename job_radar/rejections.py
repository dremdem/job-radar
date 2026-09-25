"""Rejections: a company said no. One rejection flags all of its opportunities.

The score stored on an opportunity stays the intrinsic title/location score; the
rejection penalty is applied at display time, because a rejection can arrive long
after the opportunity was imported. See docs/data-model.md.
"""

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import Opportunity, Rejection, RejectionSource, utcnow

# Big enough to visibly sink the score; the queue also sorts rejected to the bottom.
REJECTION_PENALTY = 5.0


def effective_score(score: float | None, *, rejected: bool) -> float | None:
    """Score as shown in the queue: intrinsic score minus the rejection penalty."""
    if score is None:
        return None
    if not rejected:
        return score
    return round(max(0.0, score - REJECTION_PENALTY), 1)


def latest_rejections(session: Session) -> dict[int, datetime]:
    """company_id -> most recent rejected_at, for every company that was rejected."""
    rows = session.execute(
        select(Rejection.company_id, func.max(Rejection.rejected_at)).group_by(Rejection.company_id)
    )
    return {company_id: rejected_at for company_id, rejected_at in rows}


def mark_rejected(
    session: Session,
    *,
    company_id: int,
    opportunity_id: int | None = None,
    source: RejectionSource = RejectionSource.MANUAL,
    rejected_at: datetime | None = None,
    role: str | None = None,
    email_message_id: int | None = None,
) -> Rejection:
    """Record that a company rejected the user. Idempotent per source email."""
    if email_message_id is not None:
        existing = session.scalar(
            select(Rejection).where(Rejection.email_message_id == email_message_id)
        )
        if existing is not None:
            return existing
    rejection = Rejection(
        company_id=company_id,
        opportunity_id=opportunity_id,
        source=source,
        role=role,
        email_message_id=email_message_id,
        rejected_at=rejected_at or utcnow(),
    )
    session.add(rejection)
    session.flush()
    return rejection


def clear_manual_rejection(session, company_id: int) -> int:
    """Remove manual rejections for a company (auto-detected ones stay). Returns count removed."""
    result = session.execute(
        delete(Rejection).where(
            Rejection.company_id == company_id, Rejection.source == RejectionSource.MANUAL
        )
    )
    return result.rowcount


def toggle_manual_rejection(session: Session, opportunity: Opportunity) -> bool:
    """Flip the manual rejection for the opportunity's company. Returns the new state."""
    removed = clear_manual_rejection(session, opportunity.company_id)
    if removed:
        return False
    mark_rejected(session, company_id=opportunity.company_id, opportunity_id=opportunity.id)
    return True
