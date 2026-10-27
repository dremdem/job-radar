"""Auto-ignore rules: move noise out of To review into its own state. See docs/settings.md."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .description import lacks_python, latest_description
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
DEFAULT_COUNTRIES = ("India", "Pakistan")
# LinkedIn often names only the city or the state ("Bengaluru (Remote)"), so for these
# countries their big cities and states count too (#229). Other countries: the name only.
COUNTRY_PLACES = {
    "india": (
        "Bengaluru", "Bangalore", "Hyderabad", "Mumbai", "Delhi", "New Delhi", "Gurgaon",
        "Gurugram", "Noida", "Pune", "Chennai", "Kolkata", "Ahmedabad", "Kochi", "Jaipur",
        "Lucknow", "Chandigarh", "Indore", "Coimbatore", "Mangaluru", "Mysuru",
        "Thiruvananthapuram", "Bhubaneswar", "Visakhapatnam", "Nagpur", "Vadodara", "Surat",
        "Karnataka", "Telangana", "Maharashtra", "Tamil Nadu", "Kerala", "Gujarat", "Haryana",
        "Uttar Pradesh", "West Bengal", "Andhra Pradesh", "Rajasthan", "Odisha",
    ),
    "pakistan": (
        "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Peshawar", "Multan",
        "Hyderabad", "Quetta", "Sialkot",
    ),
}  # fmt: skip


@dataclass(frozen=True)
class Rules:
    hybrid: bool = False
    applied: bool = False
    ignored: bool = False
    rejected: bool = False
    no_python: bool = False
    ai_location: bool = False  # docs/ai.md
    language: bool = False  # title or description not in English / Russian
    days: int = 90
    countries: tuple[str, ...] = DEFAULT_COUNTRIES  # empty: the rule is off (#229)

    @property
    def any(self) -> bool:
        return any(
            (
                self.hybrid,
                self.applied,
                self.ignored,
                self.rejected,
                self.no_python,
                self.ai_location,
                self.language,
                self.countries,
            )
        )


def parse_countries(text: str) -> tuple[str, ...]:
    """ "india, Pakistan\nVietnam" -> ("India", "Pakistan", "Vietnam"), without repeats."""
    found: list[str] = []
    for part in re.split(r"[,;\n]", text):
        name = " ".join(part.split())
        if name.islower():
            name = name.title()
        if name and name.casefold() not in {f.casefold() for f in found}:
            found.append(name)
    return tuple(found)


def country_of(location: str | None, countries: tuple[str, ...]) -> str | None:
    """The first listed country the location is in, by its name or a known city / state."""
    if not location:
        return None
    for country in countries:
        names = (country, *COUNTRY_PLACES.get(country.casefold(), ()))
        if any(re.search(rf"\b{re.escape(n)}\b", location, re.IGNORECASE) for n in names):
            return country
    return None


def load_rules(session: Session) -> Rules:
    row = session.get(AppSetting, SETTINGS_KEY)
    if row is None or not isinstance(row.value, dict):
        return Rules()
    known = {k: v for k, v in row.value.items() if k in Rules.__dataclass_fields__}
    if isinstance(known.get("countries"), list):  # JSON has no tuples
        known["countries"] = tuple(known["countries"])
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

    if country := country_of(opportunity.location, rules.countries):
        return f"country: {country} ({opportunity.location})"
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
        # A vacancy you marked applied in Job Radar counts too, mail or not (#211).
        marked = session.execute(
            select(Opportunity.state_changed_at, Opportunity.updated_at).where(
                Opportunity.company_id == company_id,
                Opportunity.state == OpportunityState.APPLIED,
                Opportunity.id != opportunity.id,
            )
        )
        dates = [_utc(changed or updated) for changed, updated in marked]
        latest = max([d for d in (latest, *dates) if d is not None], default=None)
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
    if rules.no_python and lacks_python(opportunity.title, latest_description(opportunity)):
        return "no Python in the description"
    if rules.hybrid and _HYBRID.search(f"{opportunity.title} {opportunity.location or ''}"):
        return "hybrid / on-site"
    if rules.language:
        from .language import vacancy_language

        if found := vacancy_language(opportunity.title, latest_description(opportunity)):
            return f"language: {found}"
    if rules.ai_location:
        from .ai.analysis import auto_ignore_reason

        if reason := auto_ignore_reason(opportunity):
            return reason
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
