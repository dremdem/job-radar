"""Checks on a vacancy's stored description. See docs/queue.md#description-checks."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from markupsafe import Markup, escape

from .models import JobSnapshot, Opportunity
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


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def latest_snapshot(opportunity: Opportunity) -> JobSnapshot | None:
    snapshots = [s for r in opportunity.source_records for s in r.snapshots]
    if not snapshots:
        return None
    # SQLite returns naive datetimes, a snapshot added in this session is aware: all are UTC.
    return max(snapshots, key=lambda s: (_utc(s.retrieved_at), s.id))


def latest_description(opportunity: Opportunity) -> str | None:
    snapshot = latest_snapshot(opportunity)
    return snapshot.raw_text if snapshot else None


def lacks_python(title: str, description: str | None) -> bool:
    """A saved description without Python, for a title that does not say Python either."""
    return bool(description) and not _PYTHON.search(title) and not _PYTHON.search(description)


@lru_cache(maxsize=8192)  # pure text check, run for every vacancy in a list (#180)
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


# On escaped text: "&" only as "&amp;", so a quote (&#34;) or "<" ends the URL.
URL = r"https?://(?:[^\s<>&()\[\]]|&amp;)+"
URL_TRAILING = ".,;:!?"  # sentence punctuation after a URL is not part of it


def link(url: str, label: str | None = None) -> str:
    """An escaped http(s) URL as a link that opens in a new tab (#241)."""
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label or url}</a>'


def _mark(escaped: str) -> str:
    return _PYTHON.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)


def highlight_python(text: str) -> Markup:
    """Escaped description: every Python mention wrapped in <mark>, links clickable (#243)."""
    escaped = str(escape(text))
    out, pos = [], 0
    for m in re.finditer(URL, escaped):
        url = m.group(0).rstrip(URL_TRAILING)
        out += [_mark(escaped[pos : m.start()]), link(url)]
        pos = m.start() + len(url)
    out.append(_mark(escaped[pos:]))
    return Markup("".join(out))
