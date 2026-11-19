"""Undo of your last decision in the queue (#56).

Every decision you make by hand goes through this module: ``change_state`` for
interested / ignore / applied, ``ignore_with_reason`` for an ignore with a reason. Each
records a ``models.UserAction`` with what the vacancy looked like before (state, when it
changed, the auto-ignore reason) and then changes the vacancy. ``last_action`` finds the
newest action not undone yet, ``describe`` says it in one line for the confirmation
("applied · Backend Engineer — Acme"), and ``undo`` puts the vacancy back and deletes the
reason note the action added. Undo refuses (``UndoConflict``) when the vacancy changed
since, so it never overwrites a newer state.
The web route is ``POST /undo`` in ``web/routes/queue.py``; the key is Z.
See docs/queue.md#undo.

Usage:
    import job_radar.review.undo as review_undo
"""

import dataclasses
import datetime as dt

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models
import job_radar.review.notes as review_notes

# How each state reads in the confirmation, as on the buttons and tabs.
STATE_LABELS = {
    models.OpportunityState.NEW: "To review",
    models.OpportunityState.INTERESTED: "interested",
    models.OpportunityState.IGNORE: "ignore",
    models.OpportunityState.APPLIED: "applied",
    models.OpportunityState.AUTO_IGNORE: "auto-ignore",
}


class UndoConflict(ValueError):
    """The vacancy changed after the action, so undoing it would overwrite something newer."""


@dataclasses.dataclass(frozen=True)
class LastAction:
    """The action ``Z`` offers to undo, with the vacancy it changed, for the confirmation."""

    action: models.UserAction
    opportunity: models.Opportunity
    text: str


def _before(opportunity: models.Opportunity, state: models.OpportunityState) -> models.UserAction:
    # What the vacancy looks like now, taken before anything changes it.
    return models.UserAction(
        opportunity_id=opportunity.id,
        from_state=opportunity.state,
        to_state=state,
        from_state_changed_at=opportunity.state_changed_at,
        from_auto_ignore_reason=opportunity.auto_ignore_reason,
    )


def change_state(
    session: sa_orm.Session, opportunity: models.Opportunity, state: models.OpportunityState
) -> models.UserAction:
    """Move the vacancy to ``state`` by hand and log the decision so it can be undone.

    Your choice replaces the rule's, so the auto-ignore reason is cleared. Does not commit:
    the caller commits.
    """
    action = _before(opportunity, state)
    session.add(action)
    opportunity.state = state
    opportunity.state_changed_at = dt.datetime.now(dt.UTC)
    opportunity.auto_ignore_reason = None
    return action


def ignore_with_reason(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    kind: models.NoteKind,
    detail: str | None = None,
    comment: str | None = None,
) -> models.UserAction:
    """``review_notes.ignore_with_reason``, logged so undo can bring the vacancy back and
    delete the reason. Raises ``ValueError`` like it; then nothing is logged."""
    action = _before(opportunity, models.OpportunityState.IGNORE)
    note = review_notes.ignore_with_reason(session, opportunity, kind, detail, comment)
    action.note_id = note.id
    session.add(action)
    session.commit()
    return action


def last_action(session: sa_orm.Session) -> LastAction | None:
    """The newest action not undone yet whose vacancy still exists, or None."""
    rows = session.execute(
        sa.select(models.UserAction, models.Opportunity)
        .join(models.Opportunity, models.Opportunity.id == models.UserAction.opportunity_id)
        .where(models.UserAction.undone_at.is_(None))
        .order_by(models.UserAction.id.desc())
        .limit(1)
    ).first()
    if rows is None:
        return None
    action, opportunity = rows
    return LastAction(action, opportunity, describe(session, action, opportunity))


def describe(
    session: sa_orm.Session, action: models.UserAction, opportunity: models.Opportunity
) -> str:
    """One line for the confirmation: "applied · Backend Engineer — Acme"."""
    what = STATE_LABELS[action.to_state]
    note = session.get(models.OpportunityNote, action.note_id) if action.note_id else None
    if note is not None:
        what = f"ignore ({review_notes.describe(note)})"
    return f"{what} · {opportunity.title} — {opportunity.company.name}"


def undo(session: sa_orm.Session, action_id: int) -> models.Opportunity:
    """Put the vacancy back as it was before the action and mark the action undone.

    Raises ``LookupError`` when there is no such action (or it is undone already) and
    ``UndoConflict`` when the vacancy is no longer in the state the action left it in.
    """
    action = session.get(models.UserAction, action_id)
    if action is None or action.undone_at is not None:
        raise LookupError("Nothing to undo")
    opportunity = session.get(models.Opportunity, action.opportunity_id)
    if opportunity is None:
        raise LookupError("The vacancy is gone")
    if opportunity.state is not action.to_state:
        raise UndoConflict(
            f"The vacancy is {STATE_LABELS[opportunity.state]} now, not "
            f"{STATE_LABELS[action.to_state]}: it changed after this action"
        )
    opportunity.state = action.from_state
    opportunity.state_changed_at = action.from_state_changed_at
    opportunity.auto_ignore_reason = action.from_auto_ignore_reason
    if action.note_id is not None and (note := session.get(models.OpportunityNote, action.note_id)):
        session.delete(note)
    action.undone_at = dt.datetime.now(dt.UTC)
    session.commit()
    return opportunity
