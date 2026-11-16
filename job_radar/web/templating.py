"""The Jinja environment of the web UI: templates, filters and globals.

Usage:
    import job_radar.web.templating as web_templating
"""

import datetime as dt
import pathlib
import re

import fastapi.templating as fastapi_templating
import markupsafe

import job_radar.review.notes as review_notes
import job_radar.scoring.description as scoring_description
import job_radar.sources.manual as sources_manual

templates = fastapi_templating.Jinja2Templates(
    directory=pathlib.Path(__file__).parent / "templates"
)
templates.env.filters["highlight_python"] = scoring_description.highlight_python


_LINK = re.compile(rf"\[([^\]]+)\]\(({scoring_description.URL})\)|({scoring_description.URL})")


def _link(m: re.Match[str]) -> str:
    """A Markdown [text](url) or a bare URL (#241); http(s) only."""
    if m.group(2):
        return scoring_description.link(m.group(2), m.group(1))
    url = m.group(3).rstrip(scoring_description.URL_TRAILING)
    return scoring_description.link(url) + m.group(3)[len(url) :]


def chat_markup(text: str) -> markupsafe.Markup:
    """Claude's light Markdown made safe: escape first, then links, **bold** and "> quote"."""
    lines = []
    for line in str(markupsafe.escape(text)).splitlines():
        line = _LINK.sub(_link, line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.startswith("&gt; "):
            line = f'<span class="quote">{line[5:]}</span>'
        lines.append(line)
    return markupsafe.Markup("\n".join(lines))  # nosec B704: built from escape(text) above


templates.env.filters["chat_markup"] = chat_markup


def pause_left(until: dt.datetime | None) -> str:
    """ "HH:MM" local while new analyses are paused, else ""."""
    if until is None or until <= dt.datetime.now(dt.UTC):
        return ""
    return local_time(until, "%H:%M")


def local_time(value: dt.datetime | None, fmt: str, tz: dt.tzinfo | None = None) -> str:
    """A stored UTC time (naive from SQLite, or aware) in the machine's local zone (#170)."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(tz).strftime(fmt)


templates.env.filters["local"] = local_time
templates.env.globals["EVENT_KINDS"] = sources_manual.EVENT_KINDS
templates.env.globals["reason_icon"] = review_notes.reason_icon
templates.env.globals["today"] = lambda: dt.datetime.now().strftime("%Y-%m-%d")
