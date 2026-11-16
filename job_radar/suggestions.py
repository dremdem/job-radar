"""Ready-made reasons to ignore a vacancy, from the checks that already ran (#117).

The checks are the vacancy language, the local model's work mode and residence findings
and Claude's skip reasons. ``suggest`` returns up to three different ones, the most
certain first; each is one tap in the queue that calls "ignore with reason" with its
kind, detail and comment. See docs/applications.md#reasons-and-notes.

Usage:
    import job_radar.suggestions as suggestions
"""

import dataclasses
import re
import typing as t

import job_radar.models as models
import job_radar.profile.stack as profile_stack
import job_radar.scoring.findings as scoring_findings
import job_radar.scoring.language as scoring_language

MAX_SUGGESTIONS = 3
# Russian names Claude uses in its gaps ("Нидерландский или французский").
_RUSSIAN_NAMES = {
    "немец": "German", "нидерланд": "Dutch", "голланд": "Dutch", "француз": "French",
    "испан": "Spanish", "итальян": "Italian", "португал": "Portuguese", "польск": "Polish",
    "китай": "Chinese", "японск": "Japanese", "корейск": "Korean", "арабск": "Arabic",
    "турецк": "Turkish", "иврит": "Hebrew", "шведск": "Swedish", "чешск": "Czech",
}  # fmt: skip


@dataclasses.dataclass(frozen=True)
class Suggestion:
    """One ready-made reason to ignore: note kind, detail, button label and comment."""

    kind: models.NoteKind
    detail: str | None
    label: str
    comment: str = ""


def _language_in(text: str) -> str | None:
    if match := scoring_language._NAME.search(text):
        return scoring_language._NAMES[match.group(1).casefold()]
    lowered = text.casefold()
    return next((lang for stem, lang in _RUSSIAN_NAMES.items() if stem in lowered), None)


def _first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    sentence = match.group(1) if match else text.strip()
    return sentence if len(sentence) <= 140 else sentence[:139] + "…"


def _language(name: str) -> Suggestion:
    return Suggestion(models.NoteKind.LANGUAGE, name, f"language: {name}")


_CLAUDE_KINDS = {
    "location": models.NoteKind.ONLY_LOCATION,
    "language": models.NoteKind.LANGUAGE,
    "stack": models.NoteKind.STACK,
    "work_format": models.NoteKind.WORK_FORMAT,
    "seniority": models.NoteKind.SENIORITY,
    "salary": models.NoteKind.SALARY,
    "contract": models.NoteKind.CONTRACT,
    "other": models.NoteKind.OTHER,
}


def _labelled(kind: models.NoteKind, detail: str, comment: str = "") -> Suggestion:
    if kind is models.NoteKind.ONLY_LOCATION:
        return Suggestion(kind, detail, f"only {detail}", comment)
    if kind is models.NoteKind.WORK_FORMAT:
        return Suggestion(kind, None, "hybrid / on-site", comment)
    if kind in (models.NoteKind.LANGUAGE, models.NoteKind.STACK) and detail:
        return Suggestion(kind, detail, f"{kind.value}: {detail}", comment)
    name = kind.value + (f": {detail}" if detail else "")
    return Suggestion(kind, None, name, comment or (f"Claude: {detail}" if detail else ""))


def _short(skill: str) -> str:
    head = re.split("\\s*[(:\u2014\u2013]\\s*|\\s+-\\s+", skill)[0].strip()
    return head if len(head) <= 30 else head[:30].rsplit(" ", 1)[0] + "…"


def _from_claude(result: dict[str, t.Any]) -> list[Suggestion]:
    if result.get("verdict") != "skip":
        return []
    reason = result.get("ignore_reason")
    if isinstance(reason, dict) and reason.get("kind") in _CLAUDE_KINDS:
        detail = str(reason.get("detail") or "").strip()[:40]
        kind = _CLAUDE_KINDS[reason["kind"]]
        if kind is models.NoteKind.ONLY_LOCATION and not detail:
            kind = models.NoteKind.OTHER
        return [_labelled(kind, detail)]
    # analyses from before ignore_reason (prompt version 1)
    out = []
    for gap in result.get("gaps") or []:
        if not isinstance(gap, dict) or not gap.get("blocker"):
            continue
        skill = str(gap.get("skill") or "").strip()
        if language := _language_in(skill):
            out.append(_language(language))
        elif skill:
            short = _short(skill)
            out.append(Suggestion(models.NoteKind.STACK, short, f"stack: {short}"))
    # no single blocker named: its summary is the reason
    if not out:
        summary = _first_sentence(str(result.get("summary") or "skip"))
        out.append(
            Suggestion(models.NoteKind.OTHER, None, f"Claude: {summary}", f"Claude: {summary}")
        )
    return out


def suggest(
    opportunity: models.Opportunity | None,
    *,
    description: str | None,
    analysis: models.DescriptionAnalysis | None = None,
    profile: profile_stack.Profile | None = None,
    claude: models.ClaudeAnalysis | None = None,
) -> list[Suggestion]:
    """Up to three different reasons, the most certain first."""
    found: list[Suggestion] = []
    title = opportunity.title if opportunity is not None else ""
    if language := scoring_language.vacancy_language(title, description):
        found.append(_language(language))
    if analysis is not None:
        if restriction := scoring_findings.residence_restriction(analysis):
            found.append(
                Suggestion(models.NoteKind.ONLY_LOCATION, restriction, f"only {restriction}")
            )
        if analysis.work_mode in ("hybrid", "onsite"):
            found.append(Suggestion(models.NoteKind.WORK_FORMAT, None, "hybrid / on-site"))
        if profile is not None:
            for item in analysis.stack or []:
                if item.get("status") == "required" and profile.level(item["skill"]) == "blocker":
                    found.append(
                        Suggestion(models.NoteKind.STACK, item["skill"], f"stack: {item['skill']}")
                    )
    if claude is not None and claude.state == "done" and isinstance(claude.result, dict):
        found.extend(_from_claude(claude.result))

    def key(s: Suggestion) -> tuple:
        # One language and one work-format reason are enough, whoever found them.
        single = s.kind in (models.NoteKind.LANGUAGE, models.NoteKind.WORK_FORMAT)
        return (s.kind, "" if single else (s.detail or "").casefold())

    unique: list[Suggestion] = []
    for s in found:
        if key(s) not in {key(u) for u in unique}:
            unique.append(s)
    return unique[:MAX_SUGGESTIONS]
