"""What the stored analysis of a vacancy means for the queue: penalties, restrictions, reasons.

The local model's answers are stored as ``DescriptionAnalysis`` (see ``ai.analysis``, which
asks the model and checks the quotes). This module only reads them: ``risks`` turns an
analysis into score penalties with a reason each (``stack_risks`` for the required
technologies), ``residence_restriction`` says where the user may not live, and
``auto_ignore_reason`` gives the reason the auto-ignore rules use. It never calls a model,
so the rules and the queue can import it without the analysis code. See docs/ai.md.

Usage:
    import job_radar.scoring.findings as scoring_findings
"""

import re

import job_radar.models as models
import job_radar.profile.home as profile_home
import job_radar.profile.stack as profile_stack
import job_radar.scoring.rules as scoring_rules

HYBRID_PENALTY = -3.0
RESIDENCE_PENALTY = -2.5
QUOTE_SHOWN = 160
# Per required technology, by its level in the profile (docs/ai.md#stack-vs-your-cv).
STACK_PENALTIES = {"unknown": -1.0, "learnable": -0.5, "blocker": -3.0}
# all non-blockers together; blockers are not capped
STACK_PENALTY_MAX = -3.0
_LEVEL_TEXT = {"unknown": "not in your CV", "learnable": "learnable", "blocker": "blocker"}
# Places anyone can work from; the user's own home (profile) is open too. Anything else in
# `residence` is a restriction.
_OPEN = re.compile(
    r"worldwide|anywhere|global|any (country|location|city)|любой|везде",
    re.IGNORECASE,
)


def stack_risks(
    analysis: models.DescriptionAnalysis | None, profile: profile_stack.Profile | None = None
) -> list[tuple[float, scoring_rules.Reason]]:
    """Required technologies by their level in the current profile (known ones drop out)."""
    if analysis is None or not analysis.stack:
        return []
    out: list[tuple[float, scoring_rules.Reason]] = []
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
        out.append((points, scoring_rules.Reason("-", text)))
    return out


def _shown(quote: str) -> str:
    quote = re.sub(r"\s+", " ", quote).strip()
    return quote if len(quote) <= QUOTE_SHOWN else quote[: QUOTE_SHOWN - 1] + "…"


def risks(
    analysis: models.DescriptionAnalysis | None, profile: profile_stack.Profile | None = None
) -> list[tuple[float, scoring_rules.Reason]]:
    if analysis is None:
        return []
    out = []
    if analysis.work_mode in ("hybrid", "onsite"):
        mode = "hybrid" if analysis.work_mode == "hybrid" else "on-site"
        text = f"AI: {mode} — “{_shown(analysis.work_mode_quote)}”"
        out.append((HYBRID_PENALTY, scoring_rules.Reason("-", text)))
    if restriction := residence_restriction(analysis):
        text = f"AI: only for {restriction} — “{_shown(analysis.residence_quote)}”"
        out.append((RESIDENCE_PENALTY, scoring_rules.Reason("-", text)))
    return out + stack_risks(analysis, profile)


def residence_restriction(
    analysis: models.DescriptionAnalysis, home: profile_home.Home | None = None
) -> str | None:
    home = profile_home.current_home() if home is None else home
    places = analysis.residence or []
    if not places or any(_OPEN.search(p) or home.listed_in(p) for p in places):
        return None
    return ", ".join(places)


def latest_analysis(opportunity: models.Opportunity) -> models.DescriptionAnalysis | None:
    return opportunity.analyses[-1] if opportunity.analyses else None


def auto_ignore_reason(opportunity: models.Opportunity) -> str | None:
    analysis = latest_analysis(opportunity)
    if analysis is None:
        return None
    if analysis.work_mode in ("hybrid", "onsite"):
        return "AI: hybrid / on-site" if analysis.work_mode == "hybrid" else "AI: on-site"
    if restriction := residence_restriction(analysis):
        return f"AI: only for {restriction}"
    return None
