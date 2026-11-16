"""Checks on a vacancy's stored description.

``latest_description`` returns the newest snapshot text. ``check_python`` finds Python
and related mentions, ``lacks_python`` and ``python_risk`` turn that into an auto-ignore
reason and a score effect, and ``highlight_python`` renders the description as safe HTML
with Python mentions marked and links clickable. See docs/queue.md#description-checks.

Usage:
    import job_radar.description as description
"""

import dataclasses
import datetime as dt
import functools
import re

import markupsafe

import job_radar.models as models
import job_radar.scoring as scoring

PYTHON_BONUS = 0.5
NO_PYTHON_PENALTY = -3.0
QUOTE_MAX = 220

# Python, Python3, python-based, Pythonic
_PYTHON = re.compile(r"\bpython", re.IGNORECASE)
_RELATED = ["Django", "FastAPI", "Flask", "asyncio", "Pydantic", "pandas", "NumPy",
            "SQLAlchemy", "Celery", "pytest", "PySpark"]  # fmt: skip
# A sentence ends at ".", "!", "?", ";" followed by a space (not "3.10"), or a line break.
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)|\n")


@dataclasses.dataclass(frozen=True)
class PythonCheck:
    """Python mentions found in a description, with a quote to show the user."""

    mentions: int
    related: list[str]
    # around the first mention, empty when there is none
    quote: str


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


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def latest_snapshot(opportunity: models.Opportunity) -> models.JobSnapshot | None:
    snapshots = [s for r in opportunity.source_records for s in r.snapshots]
    if not snapshots:
        return None
    # SQLite returns naive datetimes, a snapshot added in this session is aware: all are UTC.
    return max(snapshots, key=lambda s: (_utc(s.retrieved_at), s.id))


def latest_description(opportunity: models.Opportunity) -> str | None:
    snapshot = latest_snapshot(opportunity)
    return snapshot.raw_text if snapshot else None


def lacks_python(title: str, description: str | None) -> bool:
    """A saved description without Python, for a title that does not say Python either."""
    return bool(description) and not _PYTHON.search(title) and not _PYTHON.search(description)


# pure text check, run for every vacancy in a list (#180)
@functools.lru_cache(maxsize=8192)
def python_risk(title: str, description: str | None) -> tuple[float, scoring.Reason] | None:
    """Score effect of Python in the description; None without a description or with Python
    already in the title."""
    if not description or _PYTHON.search(title):
        return None
    check = check_python(description)
    if check.mentions == 0:
        return NO_PYTHON_PENALTY, scoring.Reason("-", "No Python in description")
    also = f"; also {', '.join(check.related)}" if check.related else ""
    return PYTHON_BONUS, scoring.Reason("+", f"Python in description (x{check.mentions}{also})")


# On escaped text: "&" only as "&amp;", so a quote (&#34;) or "<" ends the URL.
URL = r"https?://(?:[^\s<>&()\[\]]|&amp;)+"
# sentence punctuation after a URL is not part of it
URL_TRAILING = ".,;:!?"


def link(url: str, label: str | None = None) -> str:
    """An escaped http(s) URL as a link that opens in a new tab (#241)."""
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label or url}</a>'


def _mark(escaped: str) -> str:
    return _PYTHON.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)


def highlight_python(text: str) -> markupsafe.Markup:
    """Escaped description: every Python mention wrapped in <mark>, links clickable (#243)."""
    escaped = str(markupsafe.escape(text))
    out, pos = [], 0
    for m in re.finditer(URL, escaped):
        url = m.group(0).rstrip(URL_TRAILING)
        out += [_mark(escaped[pos : m.start()]), link(url)]
        pos = m.start() + len(url)
    out.append(_mark(escaped[pos:]))
    return markupsafe.Markup("".join(out))  # nosec B704: built from escape(text) above
