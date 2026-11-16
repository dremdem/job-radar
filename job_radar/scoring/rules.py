"""Rule-based score of a vacancy from its title and location.

The score starts at 5. Rules on the title and on the location (worldwide, hybrid or
on-site, remote only for places that may exclude the user's home) add or take points, and
every rule that fires leaves a ``Reason`` the queue shows. It only ranks the queue and
decides nothing. See docs/linkedin-alerts.md.

Usage:
    import job_radar.scoring.rules as scoring_rules
"""

import dataclasses
import re

import job_radar.profile.home as profile_home

BASE = 5.0


@dataclasses.dataclass(frozen=True)
class Reason:
    """One rule that fired: ``+`` or ``-`` and the text the queue shows."""

    # "+" or "-"
    sign: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"sign": self.sign, "text": self.text}


@dataclasses.dataclass(frozen=True)
class Score:
    """A vacancy's score from 0 to 10 with the reasons behind it."""

    value: float
    reasons: list[Reason]


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


# (pattern on title, points, reason)
TITLE_RULES: list[tuple[str, float, str]] = [
    (r"\b(senior|sr\.?|staff|lead|principal)\b", 1.5, "Senior level"),
    (r"\bpython\b", 2.0, "Python"),
    (r"\bback[\s-]?end\b", 1.5, "Backend"),
    (r"\b(fastapi|django|flask)\b", 1.0, "Python web framework"),
    (r"\b(platform|data)\b", 0.5, "Platform / data"),
    (r"\b(junior|jr\.?|intern|internship|trainee)\b", -4.0, "Junior / intern"),
    (r"\bfront[\s-]?end\b", -3.0, "Frontend"),
    (r"\b(full[\s-]?stack)\b", -1.0, "Full-stack"),
]
OTHER_STACKS = ["Java", ".NET", "PHP", "Ruby", "Golang", "C#", "C\\+\\+", "Scala", "Kotlin"]


def _title_reasons(title: str) -> list[tuple[float, Reason]]:
    out = [
        (points, Reason("+" if points > 0 else "-", text))
        for pattern, points, text in TITLE_RULES
        if _has(pattern, title)
    ]
    if not _has(r"\bpython\b", title):
        for stack in OTHER_STACKS:
            # \b does not work around "." / "#"; look for non-word neighbours instead.
            if _has(rf"(?<!\w){stack}(?!\w)", title):
                out.append((-3.0, Reason("-", f"{stack.replace(chr(92), '')}, no Python in title")))
                break
    return out


def _location_reasons(
    location: str | None, remote_known: bool, home: profile_home.Home
) -> list[tuple[float, Reason]]:
    if not location:
        return []
    if _has(r"worldwide|anywhere|global", location):
        return [(2.0, Reason("+", "Worldwide / anywhere"))]
    if place := home.listed_in(location):
        return [(1.0, Reason("+", f"{place} listed"))]
    if _has(r"hybrid|on-?site", location):
        return [(-3.0, Reason("-", "Hybrid / on-site"))]
    if _has(r"remote", location):
        place = re.sub(r"\(?remote\)?", "", location, flags=re.IGNORECASE).strip(" ,·-")
        if place:
            return [
                (-0.5, Reason("-", f"Remote listed for {place}: check if {home.where} is allowed"))
            ]
        return []
    if not remote_known:
        # Public search cards never show the work format; absence says nothing.
        text = f"Work format not shown for {location}: check remote and {home.where}"
        return [(-0.5, Reason("-", text))]
    return [(-2.0, Reason("-", "No remote marker in location"))]


def score_job(
    title: str,
    location: str | None,
    *,
    remote_known: bool = True,
    home: profile_home.Home | None = None,
) -> Score:
    """``remote_known=False`` when the source never shows the work format (public search).
    ``home`` defaults to the one in the profile (job_radar/profile/home.py)."""
    home = profile_home.current_home() if home is None else home
    fired = _title_reasons(title) + _location_reasons(location, remote_known, home)
    value = BASE + sum(points for points, _ in fired)
    return Score(value=round(min(10.0, max(0.0, value)), 1), reasons=[r for _, r in fired])
