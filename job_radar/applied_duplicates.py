"""A vacancy you applied to never comes back to the queue (#269, docs/queue.md).

The same vacancy is the same company and the same title (case, spaces and punctuation
ignored). When exactly one such vacancy of the company is applied, a copy from any other
source joins it: its source, description and the rest move to the applied vacancy, whose
history says "seen again". Another vacancy of the company stays as it is.
"""

import re
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import merge_opportunities
from .models import CompanyEvent, Opportunity, OpportunityState

S = OpportunityState
# Copies the batch merge takes in; "interested" is your own call and stays.
MERGEABLE = (S.NEW, S.AUTO_IGNORE, S.IGNORE)


def same_title(title: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (title or "").casefold()).split())


def applied_original(session: Session, opportunity: Opportunity) -> Opportunity | None:
    """The one applied vacancy of the company with this title; None if 0 or 2+."""
    found = [
        o
        for o in session.scalars(
            select(Opportunity).where(
                Opportunity.company_id == opportunity.company_id,
                Opportunity.state == S.APPLIED,
                Opportunity.id != opportunity.id,
            )
        )
        if same_title(o.title) == same_title(opportunity.title)
    ]
    return found[0] if len(found) == 1 else None


def _seen_again(session: Session, keep: Opportunity, dup: Opportunity, now: datetime) -> None:
    sources = ", ".join(
        f"{r.source_type.value}{' ' + r.url if r.url else ''}" for r in dup.source_records
    )
    session.add(
        CompanyEvent(
            company_id=keep.company_id,
            opportunity_id=keep.id,
            kind="seen again",
            at=now,
            comment=f"{dup.title}: {sources}" if sources else dup.title,
        )
    )


def fold_into_applied(
    session: Session, opportunity: Opportunity, now: datetime | None = None
) -> Opportunity | None:
    """Join a new vacancy to the applied one it repeats; the applied one, or None."""
    if opportunity.state is not S.NEW:
        return None
    keep = applied_original(session, opportunity)
    if keep is None:
        return None
    _seen_again(session, keep, opportunity, now or datetime.now(UTC))
    merge_opportunities(session, keep, opportunity)
    return keep


def merge_applied_duplicates(
    session: Session, *, apply: bool, skip: frozenset[int] = frozenset()
) -> list[tuple[int, list[int]]]:
    """Copies of applied vacancies already stored: (applied id, copy ids) per group. With
    apply=False nothing changes (a dry run). ``skip``: applied ids whose group stays apart."""
    groups: dict[tuple[int, str], list[Opportunity]] = defaultdict(list)
    for o in session.scalars(select(Opportunity).order_by(Opportunity.id)):
        groups[(o.company_id, same_title(o.title))].append(o)
    plan = []
    for members in groups.values():
        originals = [o for o in members if o.state is S.APPLIED]
        copies = [o for o in members if o.state in MERGEABLE]
        if len(originals) == 1 and copies and originals[0].id not in skip:
            plan.append((originals[0], copies))
    if apply:
        now = datetime.now(UTC)
        for keep, copies in plan:
            for dup in copies:
                _seen_again(session, keep, dup, now)
                merge_opportunities(session, keep, dup)
        session.commit()
    return [(keep.id, [d.id for d in copies]) for keep, copies in plan]
