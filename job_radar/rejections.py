"""Rejections: a company said no, so all its vacancies are flagged.

The score stored on a vacancy stays the intrinsic title and location score; the rejection
penalty is applied at display time by ``effective_score``, because a rejection can arrive
long after the vacancy was imported. ``mark_rejected`` records one (idempotent per
email), and the user can toggle a manual rejection from the queue. See
docs/data-model.md.

Usage:
    import job_radar.rejections as rejections
"""

import datetime as dt

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models

# Big enough to visibly sink the score; the queue also sorts rejected to the bottom.
REJECTION_PENALTY = 5.0


def effective_score(score: float | None, *, rejected: bool) -> float | None:
    """Score as shown in the queue: intrinsic score minus the rejection penalty."""
    if score is None:
        return None
    if not rejected:
        return score
    return round(max(0.0, score - REJECTION_PENALTY), 1)


def latest_rejections(session: sa_orm.Session) -> dict[int, dt.datetime]:
    """company_id -> most recent rejected_at, for every company that was rejected."""
    rows = session.execute(
        sa.select(models.Rejection.company_id, sa.func.max(models.Rejection.rejected_at)).group_by(
            models.Rejection.company_id
        )
    )
    return {company_id: rejected_at for company_id, rejected_at in rows}


def mark_rejected(
    session: sa_orm.Session,
    *,
    company_id: int,
    opportunity_id: int | None = None,
    source: models.RejectionSource = models.RejectionSource.MANUAL,
    rejected_at: dt.datetime | None = None,
    role: str | None = None,
    email_message_id: int | None = None,
) -> models.Rejection:
    """Record that a company rejected the user. Idempotent per source email."""
    if email_message_id is not None:
        existing = session.scalar(
            sa.select(models.Rejection).where(models.Rejection.email_message_id == email_message_id)
        )
        if existing is not None:
            return existing
    rejection = models.Rejection(
        company_id=company_id,
        opportunity_id=opportunity_id,
        source=source,
        role=role,
        email_message_id=email_message_id,
        rejected_at=rejected_at or models.utcnow(),
    )
    session.add(rejection)
    session.flush()
    return rejection


def clear_manual_rejection(session, company_id: int) -> int:
    """Remove manual rejections for a company (auto-detected ones stay). Returns count removed."""
    result = session.execute(
        sa.delete(models.Rejection).where(
            models.Rejection.company_id == company_id,
            models.Rejection.source == models.RejectionSource.MANUAL,
        )
    )
    return result.rowcount


def toggle_manual_rejection(session: sa_orm.Session, opportunity: models.Opportunity) -> bool:
    """Flip the manual rejection for the opportunity's company. Returns the new state."""
    removed = clear_manual_rejection(session, opportunity.company_id)
    if removed:
        return False
    mark_rejected(session, company_id=opportunity.company_id, opportunity_id=opportunity.id)
    return True
