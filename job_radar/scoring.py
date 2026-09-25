"""Rule-based score from title and location, with a reason for every rule that fired.

Heuristic only: it ranks the queue, it does not decide anything. See docs/linkedin-alerts.md.
"""

import re
from dataclasses import dataclass

BASE = 5.0


@dataclass(frozen=True)
class Reason:
    sign: str  # "+" or "-"
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"sign": self.sign, "text": self.text}


@dataclass(frozen=True)
class Score:
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


def _location_reasons(location: str | None, remote_known: bool) -> list[tuple[float, Reason]]:
    if not location:
        return []
    if _has(r"worldwide|anywhere|global", location):
        return [(2.0, Reason("+", "Worldwide / anywhere"))]
    if _has(r"thailand", location):
        return [(1.0, Reason("+", "Thailand listed"))]
    if _has(r"hybrid|on-?site", location):
        return [(-3.0, Reason("-", "Hybrid / on-site"))]
    if _has(r"remote", location):
        place = re.sub(r"\(?remote\)?", "", location, flags=re.IGNORECASE).strip(" ,·-")
        if place:
            return [(-0.5, Reason("-", f"Remote listed for {place}: check if Thailand is allowed"))]
        return []
    if not remote_known:
        # Public search cards never show the work format; absence says nothing.
        text = f"Work format not shown for {location}: check remote and Thailand"
        return [(-0.5, Reason("-", text))]
    return [(-2.0, Reason("-", "No remote marker in location"))]


def score_job(title: str, location: str | None, *, remote_known: bool = True) -> Score:
    """``remote_known=False`` when the source never shows the work format (public search)."""
    fired = _title_reasons(title) + _location_reasons(location, remote_known)
    value = BASE + sum(points for points, _ in fired)
    return Score(value=round(min(10.0, max(0.0, value)), 1), reasons=[r for _, r in fired])
