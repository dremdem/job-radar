"""The Jinja environment of the web UI: templates, filters and globals.

Usage:
    import job_radar.web.templating as web_templating
"""

import collections.abc as cabc
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


# #54: Claude fences text meant to be sent (a letter, a reply) with ``` lines.
_FENCE = re.compile(r"^\s*```[\w+-]*\s*$")


def _fenced(lines: list[str]) -> cabc.Iterator[tuple[bool, list[str]]]:
    """Runs of lines as (inside a fence, lines), fence lines dropped; an unclosed fence
    runs to the end."""
    inside, run = False, []
    for line in lines:
        if _FENCE.match(line):
            if run or inside:
                yield inside, run
            inside, run = not inside, []
        else:
            run.append(line)
    if run or inside:
        yield inside, run


def paste_text(text: str) -> str:
    """What Copy puts on the clipboard: the fenced text (blocks joined by a blank line), or
    the whole answer when it has no fence (#54)."""
    blocks = ["\n".join(run).strip("\n") for inside, run in _fenced(text.splitlines()) if inside]
    return "\n\n".join(b for b in blocks if b) or text


def _markup_line(line: str) -> str:
    """One escaped line: links, **bold** and "> quote"."""
    line = _LINK.sub(_link, line)
    line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
    if line.startswith("&gt; "):
        line = f'<span class="quote">{line[5:]}</span>'
    return line


def chat_markup(text: str) -> markupsafe.Markup:
    """Claude's light Markdown made safe: escape first, then links, **bold**, "> quote" and
    fenced text to send shown as a block without the fence lines."""
    parts = []
    for inside, run in _fenced(str(markupsafe.escape(text)).splitlines()):
        body = "\n".join(_markup_line(line) for line in run)
        parts.append(f'<span class="paste">{body.strip(chr(10))}</span>' if inside else body)
    return markupsafe.Markup("\n".join(parts))  # nosec B704: built from escape(text) above


templates.env.filters["chat_markup"] = chat_markup
templates.env.filters["paste_text"] = paste_text


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
