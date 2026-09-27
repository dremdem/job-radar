"""Checks on a vacancy's stored description. See docs/queue.md#description-checks."""

import re
from dataclasses import dataclass

from .models import Opportunity
from .scoring import Reason

PYTHON_BONUS = 0.5
NO_PYTHON_PENALTY = -3.0
QUOTE_MAX = 220

_PYTHON = re.compile(r"\bpython", re.IGNORECASE)  # Python, Python3, python-based, Pythonic
_RELATED = ["Django", "FastAPI", "Flask", "asyncio", "Pydantic", "pandas", "NumPy",
            "SQLAlchemy", "Celery", "pytest", "PySpark"]  # fmt: skip
# A sentence ends at ".", "!", "?", ";" followed by a space (not "3.10"), or a line break.
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)|\n")


@dataclass(frozen=True)
class PythonCheck:
    mentions: int
    related: list[str]
    quote: str  # around the first mention, empty when there is none


def _quote(text: str, pos: int) -> str:
    start = max((m.end() for m in _SENTENCE_END.finditer(text, 0, pos)), default=0)
    start = max(start, pos - 100)
    end_match = _SENTENCE_END.search(text, pos)
    end = min(end_match.end() if end_match else len(text), pos + 120)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    return quote[:QUOTE_MAX]


def check_python(description: str) -> PythonCheck:
    found = list(_PYTHON.finditer(description))
    related = sorted(
        (
            tool
            for tool in _RELATED
            if re.search(rf"\b{re.escape(tool)}\b", description, re.IGNORECASE)
        ),
        key=lambda tool: re.search(rf"\b{re.escape(tool)}\b", description, re.IGNORECASE).start(),
    )
    quote = _quote(description, found[0].start()) if found else ""
    return PythonCheck(mentions=len(found), related=related, quote=quote)


def latest_description(opportunity: Opportunity) -> str | None:
    snapshots = [s for r in opportunity.source_records for s in r.snapshots]
    if not snapshots:
        return None
    return max(snapshots, key=lambda s: (s.retrieved_at, s.id)).raw_text


def lacks_python(title: str, description: str | None) -> bool:
    """A saved description without Python, for a title that does not say Python either."""
    return bool(description) and not _PYTHON.search(title) and not _PYTHON.search(description)


def python_risk(title: str, description: str | None) -> tuple[float, Reason] | None:
    """Score effect of Python in the description; None without a description or with Python
    already in the title."""
    if not description or _PYTHON.search(title):
        return None
    check = check_python(description)
    if check.mentions == 0:
        return NO_PYTHON_PENALTY, Reason("-", "No Python in description")
    also = f"; also {', '.join(check.related)}" if check.related else ""
    return PYTHON_BONUS, Reason("+", f"Python in description (x{check.mentions}{also})")
