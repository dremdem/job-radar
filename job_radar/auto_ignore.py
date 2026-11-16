"""Auto-ignore rules: move noise out of To review into its own state.

The rules are chosen on the Settings page and stored as ``Rules``: hybrid or on-site work,
companies already applied to, ignored or rejected within N days, no Python in the
description, a residence restriction, another language, a location in a chosen country,
Claude's "skip" reasons and vacancies a site says are closed. Only vacancies in the
``new`` state move; the user's own states are never touched, and an auto-ignored vacancy
shows the rule that moved it.

Main entry points: ``load_rules`` / ``save_rules``, ``apply_rules`` for one vacancy,
``apply_to_review`` for the whole queue, ``apply_claude_rule`` after an analysis, and
``mark_unavailable`` / ``mark_not_accepting`` for vacancies a site says are closed. See
docs/settings.md.

Usage:
    import job_radar.auto_ignore as auto_ignore
"""

import dataclasses
import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.description as description
import job_radar.findings as findings
import job_radar.models as models

SETTINGS_KEY = "auto_ignore"
UNAVAILABLE_REASON = "vacancy not available"
NOT_ACCEPTING_REASON = "not accepting applications"
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
# The main reasons Claude names for a `skip` (claude/prompt.py), as shown in Settings (#245).
CLAUDE_KINDS = {
    "location": "location (only some country / region)",
    "work_format": "hybrid / on-site",
    "language": "language",
    "stack": "stack",
    "seniority": "seniority",
    "salary": "salary",
    "contract": "contract",
    "other": "other",
}


@dataclasses.dataclass(frozen=True)
class Rules:
    """The auto-ignore rules chosen on the Settings page; stored as JSON in ``AppSetting``."""

    hybrid: bool = False
    applied: bool = False
    ignored: bool = False
    rejected: bool = False
    no_python: bool = False
    # docs/ai.md
    ai_location: bool = False
    # title or description not in English / Russian
    language: bool = False
    days: int = 90
    # empty: the rule is off (#229)
    countries: tuple[str, ...] = DEFAULT_COUNTRIES
    # every `skip` from Claude (#245)
    claude_any: bool = False
    # or only these main reasons of a `skip`
    claude_kinds: tuple[str, ...] = ()
    # the site says not available (#276) or not accepting (#278)
    unavailable: bool = True

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
                self.claude_any,
                self.claude_kinds,
                self.unavailable,
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


def load_rules(session: sa_orm.Session) -> Rules:
    row = session.get(models.AppSetting, SETTINGS_KEY)
    if row is None or not isinstance(row.value, dict):
        return Rules()
    known = {k: v for k, v in row.value.items() if k in Rules.__dataclass_fields__}
    for name in ("countries", "claude_kinds"):
        # JSON has no tuples
        if isinstance(known.get(name), list):
            known[name] = tuple(known[name])
    return Rules(**known)


def save_rules(session: sa_orm.Session, rules: Rules) -> None:
    row = session.get(models.AppSetting, SETTINGS_KEY) or models.AppSetting(key=SETTINGS_KEY)
    row.value = dataclasses.asdict(rules)
    session.add(row)
    session.commit()


def parse_claude_kinds(values: list[str] | None) -> tuple[str, ...]:
    """Known reasons only, in the order of CLAUDE_KINDS."""
    chosen = set(values or ())
    return tuple(k for k in CLAUDE_KINDS if k in chosen)


def claude_reason(
    session: sa_orm.Session, opportunity: models.Opportunity, rules: Rules
) -> str | None:
    """`Claude: location: EU only` when Claude's latest analysis says skip for a chosen reason."""
    if not (rules.claude_any or rules.claude_kinds) or opportunity.id is None:
        return None
    result = session.scalar(
        sa.select(models.ClaudeAnalysis.result)
        .where(
            models.ClaudeAnalysis.opportunity_id == opportunity.id,
            models.ClaudeAnalysis.state == "done",
            models.ClaudeAnalysis.result.is_not(None),
        )
        .order_by(models.ClaudeAnalysis.id.desc())
        .limit(1)
    )
    if not isinstance(result, dict) or result.get("verdict") != "skip":
        return None
    reason = result.get("ignore_reason")
    kind = reason.get("kind") if isinstance(reason, dict) else None
    # no named reason: an old analysis, or "none"
    if kind not in CLAUDE_KINDS:
        return "Claude: skip" if rules.claude_any else None
    if not rules.claude_any and kind not in rules.claude_kinds:
        return None
    label = "hybrid / on-site" if kind == "work_format" else kind
    detail = " ".join(str(reason.get("detail") or "").split())[:60]
    return f"Claude: {label}" + (f": {detail}" if detail else "")


def apply_claude_rule(session: sa_orm.Session, opportunity: models.Opportunity) -> bool:
    """Run only the Claude rule, when an analysis finishes. Only `new` vacancies move."""
    if opportunity.state is not models.OpportunityState.NEW:
        return False
    reason = claude_reason(session, opportunity, load_rules(session))
    if reason is None:
        return False
    opportunity.state, opportunity.auto_ignore_reason = models.OpportunityState.AUTO_IGNORE, reason
    return True


def mark_unavailable(session: sa_orm.Session, opportunity: models.Opportunity) -> None:
    """The site says this vacancy is not available (#276): flag it, and auto-ignore it if `new`."""
    opportunity.unavailable_at = dt.datetime.now(dt.UTC)
    _auto_ignore_closed(session, opportunity, UNAVAILABLE_REASON)


def mark_not_accepting(session: sa_orm.Session, opportunity: models.Opportunity) -> None:
    """LinkedIn says it takes no applications (#278): flag it, and auto-ignore it if `new`."""
    opportunity.not_accepting_at = dt.datetime.now(dt.UTC)
    _auto_ignore_closed(session, opportunity, NOT_ACCEPTING_REASON)


def _auto_ignore_closed(
    session: sa_orm.Session, opportunity: models.Opportunity, reason: str
) -> None:
    if opportunity.state is models.OpportunityState.NEW and load_rules(session).unavailable:
        opportunity.state, opportunity.auto_ignore_reason = (
            models.OpportunityState.AUTO_IGNORE,
            reason,
        )


def clear_unavailable(opportunity: models.Opportunity) -> None:
    """Drop the flags, and undo the auto-ignore only if this rule set it."""
    opportunity.unavailable_at = opportunity.not_accepting_at = None
    if (
        opportunity.state is models.OpportunityState.AUTO_IGNORE
        and opportunity.auto_ignore_reason
        in (
            UNAVAILABLE_REASON,
            NOT_ACCEPTING_REASON,
        )
    ):
        opportunity.state, opportunity.auto_ignore_reason = models.OpportunityState.NEW, None


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def reason_for(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    rules: Rules,
    now: dt.datetime | None = None,
) -> str | None:
    """The first rule that matches, as a reason for the queue; None if none does."""
    now = now or dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=rules.days)
    company_id, company = opportunity.company_id, opportunity.company.name

    if rules.unavailable and opportunity.unavailable_at is not None:
        return UNAVAILABLE_REASON
    if rules.unavailable and opportunity.not_accepting_at is not None:
        return NOT_ACCEPTING_REASON
    if country := country_of(opportunity.location, rules.countries):
        return f"country: {country} ({opportunity.location})"
    if rules.rejected:
        latest = max(
            (
                _utc(r)
                for r in session.scalars(
                    sa.select(models.Rejection.rejected_at).where(
                        models.Rejection.company_id == company_id
                    )
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
                    sa.select(models.Application.applied_at).where(
                        models.Application.company_id == company_id,
                        models.Application.review == models.ApplicationReview.CONFIRMED,
                    )
                )
            ),
            default=None,
        )
        # A vacancy you marked applied in Job Radar counts too, mail or not (#211).
        marked = session.execute(
            sa.select(models.Opportunity.state_changed_at, models.Opportunity.updated_at).where(
                models.Opportunity.company_id == company_id,
                models.Opportunity.state == models.OpportunityState.APPLIED,
                models.Opportunity.id != opportunity.id,
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
                    sa.select(models.Opportunity.updated_at).where(
                        models.Opportunity.company_id == company_id,
                        models.Opportunity.state == models.OpportunityState.IGNORE,
                        models.Opportunity.id != opportunity.id,
                    )
                )
            ),
            default=None,
        )
        if latest and latest >= since:
            return f"ignored another {company} vacancy on {latest:%Y-%m-%d}"
    if rules.no_python and description.lacks_python(
        opportunity.title, description.latest_description(opportunity)
    ):
        return "no Python in the description"
    if rules.hybrid and _HYBRID.search(f"{opportunity.title} {opportunity.location or ''}"):
        return "hybrid / on-site"
    if rules.language:
        import job_radar.language as language

        if found := language.vacancy_language(
            opportunity.title, description.latest_description(opportunity)
        ):
            return f"language: {found}"
    if reason := claude_reason(session, opportunity, rules):
        return reason
    if rules.ai_location and (reason := findings.auto_ignore_reason(opportunity)):
        return reason
    return None


def apply_rules(
    session: sa_orm.Session, opportunity: models.Opportunity, rules: Rules | None = None
) -> bool:
    """Auto-ignore a `new` vacancy if a rule matches. Your own states are never touched.

    First, a copy of a vacancy you applied to joins that one and leaves the queue (#269);
    True then too, and the caller's record now belongs to the applied vacancy."""
    import job_radar.applied_duplicates as applied_duplicates

    if applied_duplicates.fold_into_applied(session, opportunity) is not None:
        return True
    rules = rules or load_rules(session)
    if not rules.any or opportunity.state is not models.OpportunityState.NEW:
        return False
    reason = reason_for(session, opportunity, rules)
    if reason is None:
        return False
    opportunity.state = models.OpportunityState.AUTO_IGNORE
    opportunity.auto_ignore_reason = reason
    return True


def apply_to_review(session: sa_orm.Session, now: dt.datetime | None = None) -> int:
    """Run the rules over every vacancy currently in To review. Returns how many moved."""
    rules = load_rules(session)
    if not rules.any:
        return 0
    moved = 0
    for o in session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.state == models.OpportunityState.NEW)
    ).all():
        reason = reason_for(session, o, rules, now)
        if reason:
            o.state, o.auto_ignore_reason = models.OpportunityState.AUTO_IGNORE, reason
            moved += 1
    session.commit()
    return moved
