"""Auto-ignore rules: move noise out of To review into its own state. See docs/settings.md."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Application,
    ApplicationReview,
    AppSetting,
    Opportunity,
    OpportunityState,
    Rejection,
)

SETTINGS_KEY = "auto_ignore"
_HYBRID = re.compile(r"\b(hybrid|on-?site)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Rules:
    hybrid: bool = False
    applied: bool = False
    ignored: bool = False
    rejected: bool = False
    days: int = 90

    @property
    def any(self) -> bool:
        return self.hybrid or self.applied or self.ignored or self.rejected


def load_rules(session: Session) -> Rules:
    row = session.get(AppSetting, SETTINGS_KEY)
    if row is None or not isinstance(row.value, dict):
        return Rules()
    known = {k: v for k, v in row.value.items() if k in Rules.__dataclass_fields__}
    return Rules(**known)


def save_rules(session: Session, rules: Rules) -> None:
    row = session.get(AppSetting, SETTINGS_KEY) or AppSetting(key=SETTINGS_KEY)
    row.value = asdict(rules)
    session.add(row)
    session.commit()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def reason_for(
    session: Session, opportunity: Opportunity, rules: Rules, now: datetime | None = None
) -> str | None:
    """The first rule that matches, as a reason for the queue; None if none does."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=rules.days)
    company_id, company = opportunity.company_id, opportunity.company.name

    if rules.rejected:
        latest = max(
            (
                _utc(r)
                for r in session.scalars(
                    select(Rejection.rejected_at).where(Rejection.company_id == company_id)
                )
            ),
            default=None,
        )
        if latest and latest >= since:
            return f"{company} rejected you on {latest:%Y-%m-%d}"
    if rules.applied:
        latest = max(
            (
                _utc(a)
                for a in session.scalars(
                    select(Application.applied_at).where(
                        Application.company_id == company_id,
                        Application.review == ApplicationReview.CONFIRMED,
                    )
                )
            ),
            default=None,
        )
        if latest and latest >= since:
            return f"applied to {company} on {latest:%Y-%m-%d}"
    if rules.ignored:
        latest = max(
            (
                _utc(t)
                for t in session.scalars(
                    select(Opportunity.updated_at).where(
                        Opportunity.company_id == company_id,
                        Opportunity.state == OpportunityState.IGNORE,
                        Opportunity.id != opportunity.id,
                    )
                )
            ),
            default=None,
        )
        if latest and latest >= since:
            return f"ignored another {company} vacancy on {latest:%Y-%m-%d}"
    if rules.hybrid and _HYBRID.search(f"{opportunity.title} {opportunity.location or ''}"):
        return "hybrid / on-site"
    return None


def apply_rules(session: Session, opportunity: Opportunity, rules: Rules | None = None) -> bool:
    """Auto-ignore a `new` vacancy if a rule matches. Your own states are never touched."""
    rules = rules or load_rules(session)
    if not rules.any or opportunity.state is not OpportunityState.NEW:
        return False
    reason = reason_for(session, opportunity, rules)
    if reason is None:
        return False
    opportunity.state = OpportunityState.AUTO_IGNORE
    opportunity.auto_ignore_reason = reason
    return True


def apply_to_review(session: Session, now: datetime | None = None) -> int:
    """Run the rules over every vacancy currently in To review. Returns how many moved."""
    rules = load_rules(session)
    if not rules.any:
        return 0
    moved = 0
    for o in session.scalars(
        select(Opportunity).where(Opportunity.state == OpportunityState.NEW)
    ).all():
        reason = reason_for(session, o, rules, now)
        if reason:
            o.state, o.auto_ignore_reason = OpportunityState.AUTO_IGNORE, reason
            moved += 1
    session.commit()
    return moved
