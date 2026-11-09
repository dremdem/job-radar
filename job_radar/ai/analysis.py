"""Work mode and residence from a description: the model extracts, code checks and decides.

See docs/ai.md.
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..description import latest_snapshot
from ..home import Home, current_home
from ..models import DescriptionAnalysis, Opportunity, OpportunityState
from ..scoring import Reason
from ..stack import Profile, Tech, known_alternative, says_optional, unknown_mentions

HYBRID_PENALTY = -3.0
RESIDENCE_PENALTY = -2.5
MAX_TEXT = 10000
QUOTE_SHOWN = 160
# Per required technology, by its level in the profile (docs/ai.md#stack-vs-your-cv).
STACK_PENALTIES = {"unknown": -1.0, "learnable": -0.5, "blocker": -3.0}
STACK_PENALTY_MAX = -3.0  # all non-blockers together; blockers are not capped
_LEVEL_TEXT = {"unknown": "not in your CV", "learnable": "learnable", "blocker": "blocker"}
MAX_STACK_QUESTIONS = 8
# Places anyone can work from; the user's own home (profile) is open too. Anything else in
# `residence` is a restriction.
_OPEN = re.compile(
    r"worldwide|anywhere|global|any (country|location|city)|любой|везде",
    re.IGNORECASE,
)
# The quote lists who is excluded or preferred, not where one must live (measured, #93).
_NOT_A_REQUIREMENT = re.compile(
    r"\bexcept\b|\bexcluding\b|\bprefer|\bideally\b|\bnice to have\b|кроме|предпочт",
    re.IGNORECASE,
)
_LETTER = re.compile(r"[^\W\d_]")  # a place has letters; a flag emoji alone is not one

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_mode": {"enum": ["remote", "hybrid", "onsite", "unknown"]},
        "work_mode_quote": {"type": "string"},
        "residence": {"type": "array", "items": {"type": "string"}},
        "residence_quote": {"type": "string"},
    },
    "required": ["work_mode", "work_mode_quote", "residence", "residence_quote"],
}
# Measured on the user's labelled vacancies (#93). No example places on purpose: a 4B model
# copies them into its answers.
PROMPT = """You read a job description. Answer with facts stated in the text only, and copy \
the exact sentence that states each fact.

work_mode: "remote" if the employee can work fully remotely (an optional office or occasional \
team meetups still count as remote); "hybrid" if the employee must come to an office on some \
days; "onsite" if they must work at an office; "unknown" if the text does not say.
residence: countries, regions or cities where the EMPLOYEE must live or be legally allowed to \
work. Not where the company, its customers or its offices are. Empty list if the job is open \
worldwide or the text does not restrict it.
Quotes: copy them exactly from the text, one sentence or line; empty string if there is no \
such sentence.

Job description:
{text}"""


STACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["required", "nice_to_have", "not_needed"]},
        "quote": {"type": "string"},
    },
    "required": ["status", "quote"],
}
STACK_PROMPT = """Job description:
{text}

Question: does this job REQUIRE {skill} ({what})? "required" if the candidate must already know \
it (listed in requirements / must-have / "you have"); "nice_to_have" if it is a plus, bonus, \
preferred or optional; "not_needed" if it is only mentioned (company stack, product, other \
teams) or not about {skill}.
Copy the sentence that shows it exactly into quote."""


class Model(Protocol):
    model: str

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    """The quote is in the text, up to case, spacing and small slips (85 % of it at one place)."""
    q = _norm(quote.replace("...", " ").replace("…", " ").strip(' ."'))
    if len(q) < 3:
        return False
    t = _norm(text)
    if q in t:
        return True
    # Anchor on the longest common piece, then compare the whole quote with that place.
    match = difflib.SequenceMatcher(None, t, q, autojunk=False).find_longest_match(
        0, len(t), 0, len(q)
    )
    start = max(0, match.a - match.b)
    window = t[start : start + len(q) + 10]
    blocks = difflib.SequenceMatcher(None, window, q, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) >= 0.85 * len(q)


@dataclass(frozen=True)
class Facts:
    work_mode: str = "unknown"
    work_mode_quote: str = ""
    residence: list[str] = field(default_factory=list)
    residence_quote: str = ""


def verify(answer: dict[str, Any], text: str) -> Facts:
    """Keep only what the description supports; drop the rest."""
    mode = str(answer.get("work_mode") or "unknown")
    mode_quote = str(answer.get("work_mode_quote") or "")
    if mode not in ("remote", "hybrid", "onsite") or not quote_in_text(mode_quote, text):
        mode, mode_quote = "unknown", ""
    places = answer.get("residence") or []
    places = [places] if isinstance(places, str) else places
    residence_quote = str(answer.get("residence_quote") or "")
    lowered = text.lower()
    residence = [
        str(p).strip()
        for p in places
        if _LETTER.search(str(p)) and str(p).strip().lower() in lowered
    ]
    if (
        not residence
        or not quote_in_text(residence_quote, text)
        or _NOT_A_REQUIREMENT.search(residence_quote)
    ):
        residence, residence_quote = [], ""
    return Facts(mode, mode_quote, residence, residence_quote)


def extract(text: str, model: Model) -> tuple[Facts, dict[str, Any]]:
    text = text[:MAX_TEXT]
    answer = model.generate_json(PROMPT.format(text=text), SCHEMA)
    return verify(answer, text), answer


def check_stack(
    text: str, model: Model, known: set[str], extra: list[Tech] | None = None
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Unknown technologies the text mentions, each judged by the model and checked by code."""
    text = text[:MAX_TEXT]
    stack: list[dict[str, str]] = []
    raw: dict[str, Any] = {}
    for tech in unknown_mentions(text, known, extra)[:MAX_STACK_QUESTIONS]:
        prompt = STACK_PROMPT.format(text=text, skill=tech.name, what=tech.what)
        answer = model.generate_json(prompt, STACK_SCHEMA)
        raw[tech.name] = answer
        status = str(answer.get("status") or "not_needed")
        quote = str(answer.get("quote") or "")
        if status not in ("required", "nice_to_have"):
            status = "not_needed"
        elif not quote_in_text(quote, text) or not tech.search(quote):
            status, quote = "not_needed", ""  # unsupported: don't invent a requirement
        elif status == "required" and known_alternative(quote, known):
            status = "alternative"
        elif status == "required" and says_optional(quote):
            status = "nice_to_have"
        stack.append({"skill": tech.name, "status": status, "quote": quote})
    return stack, raw


def stack_risks(
    analysis: DescriptionAnalysis | None, profile: Profile | None = None
) -> list[tuple[float, Reason]]:
    """Required technologies by their level in the current profile (known ones drop out)."""
    if analysis is None or not analysis.stack:
        return []
    out: list[tuple[float, Reason]] = []
    left = STACK_PENALTY_MAX
    for item in analysis.stack:
        if item.get("status") != "required":
            continue
        level = profile.level(item["skill"]) if profile else "unknown"
        if level == "known":
            continue
        points = STACK_PENALTIES[level]
        if level != "blocker":
            points = max(points, left) if left < 0 else 0.0
            left -= points
        text = (
            f"Requires {item['skill']} ({_LEVEL_TEXT[level]}) — “{_shown(item.get('quote', ''))}”"
        )
        out.append((points, Reason("-", text)))
    return out


def _shown(quote: str) -> str:
    quote = re.sub(r"\s+", " ", quote).strip()
    return quote if len(quote) <= QUOTE_SHOWN else quote[: QUOTE_SHOWN - 1] + "…"


def risks(
    analysis: DescriptionAnalysis | None, profile: Profile | None = None
) -> list[tuple[float, Reason]]:
    if analysis is None:
        return []
    out = []
    if analysis.work_mode in ("hybrid", "onsite"):
        mode = "hybrid" if analysis.work_mode == "hybrid" else "on-site"
        text = f"AI: {mode} — “{_shown(analysis.work_mode_quote)}”"
        out.append((HYBRID_PENALTY, Reason("-", text)))
    if restriction := residence_restriction(analysis):
        text = f"AI: only for {restriction} — “{_shown(analysis.residence_quote)}”"
        out.append((RESIDENCE_PENALTY, Reason("-", text)))
    return out + stack_risks(analysis, profile)


def residence_restriction(analysis: DescriptionAnalysis, home: Home | None = None) -> str | None:
    home = current_home() if home is None else home
    places = analysis.residence or []
    if not places or any(_OPEN.search(p) or home.listed_in(p) for p in places):
        return None
    return ", ".join(places)


def latest_analysis(opportunity: Opportunity) -> DescriptionAnalysis | None:
    return opportunity.analyses[-1] if opportunity.analyses else None


def auto_ignore_reason(opportunity: Opportunity) -> str | None:
    analysis = latest_analysis(opportunity)
    if analysis is None:
        return None
    if analysis.work_mode in ("hybrid", "onsite"):
        return "AI: hybrid / on-site" if analysis.work_mode == "hybrid" else "AI: on-site"
    if restriction := residence_restriction(analysis):
        return f"AI: only for {restriction}"
    return None


@dataclass
class AnalysisStats:
    analyzed: int = 0
    flagged: int = 0
    errors: int = 0
    pending: int = 0  # left for the next run (limit)
    last_error: str = ""


def pending(session: Session, *, with_stack: bool = False) -> list[Opportunity]:
    """To review vacancies whose latest description lacks an analysis (or, with a profile, its
    stack check), newest first."""
    stack_done = dict(
        session.execute(select(DescriptionAnalysis.snapshot_id, DescriptionAnalysis.stack)).all()
    )
    out = []
    for o in session.scalars(
        select(Opportunity)
        .where(Opportunity.state == OpportunityState.NEW)
        .order_by(Opportunity.id.desc())
    ):
        snapshot = latest_snapshot(o)
        if snapshot is None:
            continue
        if snapshot.id not in stack_done or (with_stack and stack_done[snapshot.id] is None):
            out.append(o)
    return out


def analyze_pending(
    session: Session,
    model: Model,
    *,
    limit: int = 150,
    known: set[str] | None = None,
    profile: Profile | None = None,
) -> AnalysisStats:
    """Work mode and residence for new descriptions; the stack check when `known` is given."""
    from ..auto_ignore import apply_rules

    if profile is not None:
        known = profile.known_set
    extra = profile.extra_techs() if profile else None
    stats = AnalysisStats()
    todo = pending(session, with_stack=known is not None)
    stats.pending = max(0, len(todo) - limit)
    for opportunity in todo[:limit]:
        snapshot = latest_snapshot(opportunity)
        analysis = session.scalar(
            select(DescriptionAnalysis).where(DescriptionAnalysis.snapshot_id == snapshot.id)
        )
        try:
            if analysis is None:
                facts, answer = extract(snapshot.raw_text, model)
                analysis = DescriptionAnalysis(
                    snapshot_id=snapshot.id,
                    opportunity=opportunity,
                    model=model.model,
                    work_mode=facts.work_mode,
                    work_mode_quote=facts.work_mode_quote,
                    residence=facts.residence,
                    residence_quote=facts.residence_quote,
                    raw=answer,
                )
            if known is not None:
                stack, raw = check_stack(snapshot.raw_text, model, known, extra)
                analysis.stack = stack
                analysis.raw = {**(analysis.raw or {}), "stack": raw}
        except Exception as e:  # one bad answer must not stop the rest
            session.rollback()
            stats.errors += 1
            stats.last_error = str(e)
            continue
        session.add(analysis)
        session.flush()
        stats.analyzed += 1
        stats.flagged += bool(risks(analysis, profile))
        apply_rules(session, opportunity)
        session.commit()
    return stats


def recheck_stack(session: Session) -> int:
    """Forget the stack check of To review vacancies, so the next run asks with the new profile."""
    rows = session.scalars(
        select(DescriptionAnalysis)
        .join(Opportunity, Opportunity.id == DescriptionAnalysis.opportunity_id)
        .where(Opportunity.state == OpportunityState.NEW, DescriptionAnalysis.stack.is_not(None))
    ).all()
    for row in rows:
        row.stack = None
    session.commit()
    return len(rows)
