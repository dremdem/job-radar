import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import applications as apps
from .. import manual, pipeline
from ..ai.analysis import latest_analysis, recheck_stack
from ..ai.analysis import risks as ai_risks
from ..auto_ignore import (
    CLAUDE_KINDS,
    Rules,
    apply_to_review,
    clear_unavailable,
    load_rules,
    parse_claude_kinds,
    parse_countries,
    save_rules,
)
from ..claude import analysis as claude
from ..claude import memory as claude_memory
from ..claude.analysis import ClaudeSettings
from ..claude.analysis import Worker as ClaudeWorker
from ..claude.prompt import quick_prompts
from ..claude.runner import ClaudeCli
from ..claude.runner import Runner as ClaudeRunner
from ..config import Settings, get_settings
from ..contacts import company_history, delete_history_item, other_vacancies
from ..db import make_engine, make_session_factory, upgrade_database
from ..description import (
    URL,
    URL_TRAILING,
    check_python,
    highlight_python,
    latest_description,
    link,
    python_risk,
)
from ..language import language_risk
from ..linkedin.alerts import LINKEDIN_SOURCES
from ..linkedin.capture import import_capture
from ..linkedin.chrome import load_chrome_pause, save_chrome_pause
from ..linkedin.public import PauseSettings, load_pause, save_pause
from ..models import (
    FILL_EVENT_KINDS,
    Application,
    ApplicationReview,
    AppSetting,
    ClaudeMessage,
    Company,
    CompanyEvent,
    EmailMessage,
    JobSnapshot,
    LinkedInFetch,
    NoteKind,
    Opportunity,
    OpportunityNote,
    OpportunityState,
    Rejection,
    SourceRecord,
)
from ..notes import (
    add_note,
    country_from_location,
    delete_note,
    ignore_with_reason,
    location_penalty,
    reason_icon,
)
from ..rejections import effective_score, latest_rejections, toggle_manual_rejection
from ..skills import SORTS, build_report, sort_rows
from ..stack import LISTS, Profile, current_profile, load_profile, save_profile
from ..suggestions import suggest
from .description_job import DescriptionJob
from .sync_job import Runner, SyncJob

HISTORY_SHOWN = 8
# What the bookmarklet reads, shared with the Chrome fill (#247): one reader for both.
CAPTURE_READ = (Path(__file__).parent / "capture_read.js").read_text()
BOOKMARKLETS = {
    name: (Path(__file__).parent / f"{name}.js").read_text().replace("__READ__", CAPTURE_READ)
    for name in ("bookmarklet", "bookmarklet_debug")
}


def bookmarklet(base_url: str, name: str = "bookmarklet") -> str:
    """The bookmarklet as a javascript: URL; comments and indentation stripped."""
    code = re.sub(r"/\*.*?\*/", "", BOOKMARKLETS[name], flags=re.S)
    code = " ".join(line.strip() for line in code.splitlines() if line.strip())
    return "javascript:" + code.replace("__BASE__", base_url.rstrip("/"))


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["highlight_python"] = highlight_python


_LINK = re.compile(rf"\[([^\]]+)\]\(({URL})\)|({URL})")


def _link(m: re.Match[str]) -> str:
    """A Markdown [text](url) or a bare URL (#241); http(s) only."""
    if m.group(2):
        return link(m.group(2), m.group(1))
    url = m.group(3).rstrip(URL_TRAILING)
    return link(url) + m.group(3)[len(url) :]


def chat_markup(text: str) -> Markup:
    """Claude's light Markdown made safe: escape first, then links, **bold** and "> quote"."""
    lines = []
    for line in str(escape(text)).splitlines():
        line = _LINK.sub(_link, line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.startswith("&gt; "):
            line = f'<span class="quote">{line[5:]}</span>'
        lines.append(line)
    return Markup("\n".join(lines))  # nosec B704: built from escape(text) above


templates.env.filters["chat_markup"] = chat_markup


def _pause_left(until: datetime | None) -> str:
    """ "HH:MM" local while new analyses are paused, else ""."""
    if until is None or until <= datetime.now(UTC):
        return ""
    return local_time(until, "%H:%M")


def local_time(value: datetime | None, fmt: str, tz: tzinfo | None = None) -> str:
    """A stored UTC time (naive from SQLite, or aware) in the machine's local zone (#170)."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime(fmt)


templates.env.filters["local"] = local_time
templates.env.globals["EVENT_KINDS"] = manual.EVENT_KINDS
templates.env.globals["reason_icon"] = reason_icon
templates.env.globals["today"] = lambda: datetime.now().strftime("%Y-%m-%d")

# Queue order: undecided first, then the ones worth a look.
_STATE_ORDER = [
    OpportunityState.NEW,
    OpportunityState.INTERESTED,
    OpportunityState.APPLIED,
    OpportunityState.IGNORE,
    OpportunityState.AUTO_IGNORE,
]


# Queue tabs: key -> (label, states shown; None = all).
TABS: dict[str, tuple[str, list[OpportunityState] | None]] = {
    "review": ("To review", [OpportunityState.NEW]),
    "interested": ("Interested", [OpportunityState.INTERESTED]),
    "applied": ("Applied", [OpportunityState.APPLIED]),
    "ignored": ("Ignored", [OpportunityState.IGNORE]),
    "auto": ("Auto-ignored", [OpportunityState.AUTO_IGNORE]),
    "all": ("All", None),
}
DEFAULT_TAB = "review"
PER_PAGE = (25, 50, 100, 200)
DEFAULT_PER_PAGE = 50


@dataclass(frozen=True)
class Filters:
    """Queue filters: any part of the text, case-insensitive (non-ASCII too)."""

    country: str = ""
    company: str = ""
    title: str = ""
    q: str = ""  # the inbox search box: title, company or location (#158)
    desc: str = ""  # "none" / "has": vacancies without / with a saved description (#176)
    per: str = ""  # vacancies per page, one of PER_PAGE; "" = the default (#180)
    sort: str = ""  # "" = score, "claude" = Claude match % (#207), "recent" = last action (#199)

    def matches(
        self,
        location: str | None,
        company: str,
        title: str = "",
        described: bool | None = None,
    ) -> bool:
        def has(needle: str, text: str | None) -> bool:
            return needle.strip().casefold() in (text or "").casefold()

        anywhere = " ".join((title, company, location or ""))
        wanted = {"none": False, "has": True}.get(self.desc)
        return (
            has(self.country, location)
            and has(self.company, company)
            and has(self.title, title)
            and has(self.q, anywhere)
            and (wanted is None or described is None or described == wanted)
        )

    def matches_opportunity(self, o: Opportunity) -> bool:
        described = any(r.snapshots for r in o.source_records)
        return self.matches(o.location, o.company.name, o.title, described)

    def with_desc(self, desc: str) -> "Filters":
        return replace(self, desc=desc)

    def with_sort(self, sort: str) -> "Filters":
        return replace(self, sort=sort)

    def with_per(self, per: int) -> "Filters":
        return replace(self, per="" if per == DEFAULT_PER_PAGE else str(per))

    @property
    def per_page(self) -> int:
        return int(self.per) if self.per in {str(n) for n in PER_PAGE} else DEFAULT_PER_PAGE

    def href(self, tab: str) -> str:
        pairs = (
            ("q", self.q),
            ("title", self.title),
            ("country", self.country),
            ("company", self.company),
            ("desc", self.desc),
            ("per", self.per),
            ("sort", self.sort),
        )
        return "/?" + urlencode({"tab": tab} | {k: v for k, v in pairs if v})


def _tab(value: str | None) -> str:
    return value if value in TABS else DEFAULT_TAB


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        # Row helpers below read per-user files (the profile) through the session.
        session.info["settings"] = request.app.state.settings
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


QUEUE_REDRAW = "jr-refresh-queue"


def _redraw_when_over(
    response: HTMLResponse, polled: bool, state: str, saved: bool
) -> HTMLResponse:
    """The poll that sees a job end, while the page watched it run, asks the queue to redraw
    its list and counters (#263). A page loaded later already shows the new state."""
    if polled and saved and state in ("done", "failed"):
        response.headers["HX-Trigger"] = QUEUE_REDRAW
    return response


def create_app(
    settings: Settings | None = None,
    sync_runner: Runner | None = None,
    claude_runner: ClaudeRunner | None = None,
    start_worker: bool = True,
    description_runner: Callable[[DescriptionJob], None] | None = None,
    chrome_runner: Callable[[DescriptionJob], None] | None = None,
) -> FastAPI:
    """``start_worker=False``: tests drive the Claude worker with ``run_once``."""
    settings = settings or get_settings()
    sync_job = SyncJob()
    description_job = DescriptionJob()
    description_runner = description_runner or (lambda job: pipeline.description_job(settings, job))

    def start_descriptions() -> bool:
        if chrome_job.state == "running":  # one LinkedIn description run at a time
            return False
        return description_job.start(description_runner)

    # The Chrome fill (#247): descriptions read in your own Chrome; tests pass a fake.
    chrome_job = DescriptionJob(blocked_note="stopped: {reason}")
    chrome_runner = chrome_runner or (lambda job: pipeline.chrome_fill_job(settings, job))

    # The Sync button runs the same pipeline as `./jr sync`, but leaves the slow LinkedIn
    # descriptions to the background job (#205); tests pass a fake.
    sync_runner = sync_runner or (
        lambda echo, on_steps: pipeline.run_sync(
            settings, echo, on_steps=on_steps, start_descriptions=start_descriptions
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # New code may bring new tables; the server must not wait for a CLI command.
        upgrade_database(settings.database_url)
        engine = make_engine(settings.database_url)
        app.state.session_factory = make_session_factory(engine)
        app.state.settings = settings
        worker = ClaudeWorker(
            app.state.session_factory,
            claude_runner or ClaudeCli(settings.claude_command, settings.claude_workdir),
            profile_path=settings.profile,
            cv_path=settings.cv_path,
        )
        app.state.claude_worker = worker
        # The status line says when a usage-limit pause ends (#197).
        templates.env.globals["claude_paused_until"] = lambda: _pause_left(worker.paused_until)
        app.state.claude_worker_on = start_worker and settings.claude_worker
        if app.state.claude_worker_on:
            worker.start()
        yield
        worker.stop()
        engine.dispose()

    app = FastAPI(title="Job Radar", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def queue(
        request: Request,
        session: SessionDep,
        tab: str = DEFAULT_TAB,
        country: str = "",
        company: str = "",
        title: str = "",
        q: str = "",
        desc: str = "",
        per: str = "",
        sort: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        return templates.TemplateResponse(
            request, "queue.html", _queue_context(session, _tab(tab), filters, page)
        )

    @app.post("/opportunities/{opportunity_id}/state", response_class=HTMLResponse)
    def set_state(
        request: Request,
        opportunity_id: int,
        state: Annotated[OpportunityState, Form()],
        session: SessionDep,
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
        source: Annotated[str, Form(alias="from")] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        tab = _tab(tab)
        opportunity = _get_opportunity(session, opportunity_id)
        opportunity.state = state
        opportunity.state_changed_at = datetime.now(UTC)
        opportunity.auto_ignore_reason = None  # your choice replaces the rule's
        session.commit()
        if source == "page":  # the vacancy page: redraw its buttons only
            context = {"o": opportunity, "states": _manual_states()}
            return templates.TemplateResponse(request, "_page_state.html", context)
        if source == "panel":  # the inbox panel (#158)
            return _panel_response(request, session, opportunity, tab, filters)
        # Replace only this row (or remove it when it leaves the tab) and refresh the tab counts
        # out of band: re-rendering the whole queue made the page jump to the end (#70).
        states = TABS[tab][1]
        context = _row_context(session, opportunity) | _tabs_context(session, tab, filters)
        context["show_row"] = states is None or opportunity.state in states
        return templates.TemplateResponse(request, "_row_and_tabs.html", context)

    @app.post("/opportunities/{opportunity_id}/unavailable/clear")
    def clear_unavailable_mark(opportunity_id: int, session: SessionDep) -> RedirectResponse:
        # The cross on the "unavailable" badge: the next Chrome fill may try it again (#276).
        opportunity = _get_opportunity(session, opportunity_id)
        clear_unavailable(opportunity)
        session.commit()
        return RedirectResponse(f"/opportunities/{opportunity.id}", status_code=303)

    @app.get("/opportunities/{opportunity_id}/row", response_class=HTMLResponse)
    def opportunity_row(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        tab: str = DEFAULT_TAB,
        country: str = "",
        company: str = "",
        title: str = "",
        q: str = "",
        desc: str = "",
        per: str = "",
        sort: str = "",
        notify: bool = False,
    ) -> HTMLResponse:
        # One row for live updates (e.g. a description saved by the bookmarklet in another tab).
        tab, filters = _tab(tab), Filters(country, company, title, q, desc, per, sort)
        opportunity = _get_opportunity(session, opportunity_id)
        states = TABS[tab][1]
        context = _row_context(session, opportunity) | _tabs_context(session, tab, filters)
        in_tab = states is None or opportunity.state in states
        context["show_row"] = in_tab and filters.matches_opportunity(opportunity)
        # After a capture: say where a vacancy went when it left this tab (#162).
        if notify and not in_tab:
            context["moved_to"] = next(
                label for label, states in TABS.values() if states and opportunity.state in states
            )
        return templates.TemplateResponse(request, "_row_and_tabs.html", context)

    @app.get("/opportunities/{opportunity_id}/panel", response_class=HTMLResponse)
    def opportunity_panel(
        request: Request, opportunity_id: int, session: SessionDep, tab: str = DEFAULT_TAB
    ) -> HTMLResponse:
        # The inbox's right-hand panel (#158): the vacancy page's content.
        opportunity = _get_opportunity(session, opportunity_id)
        context = _row_context(session, opportunity) | _claude_context(session, opportunity_id)
        context |= {"tab": _tab(tab), "mode": "panel"}
        return templates.TemplateResponse(request, "_detail.html", context)

    @app.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
    def opportunity_page(
        request: Request, opportunity_id: int, session: SessionDep
    ) -> HTMLResponse:
        opportunity = _get_opportunity(session, opportunity_id)
        context = _row_context(session, opportunity) | _claude_context(session, opportunity_id)
        context |= {"tab": DEFAULT_TAB, "mode": "page"}
        return templates.TemplateResponse(request, "opportunity.html", context)

    @app.get("/opportunities/{opportunity_id}/claude", response_class=HTMLResponse)
    def opportunity_claude(
        request: Request, opportunity_id: int, session: SessionDep
    ) -> HTMLResponse:
        _get_opportunity(session, opportunity_id)
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post("/opportunities/{opportunity_id}/claude", response_class=HTMLResponse)
    def opportunity_analyze(
        request: Request, opportunity_id: int, session: SessionDep
    ) -> HTMLResponse:
        _get_opportunity(session, opportunity_id)
        claude.enqueue(session, [opportunity_id])
        request.app.state.claude_worker.wake()
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post("/opportunities/{opportunity_id}/chat", response_class=HTMLResponse)
    def opportunity_ask(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        text: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        _get_opportunity(session, opportunity_id)
        if claude.ask(session, opportunity_id, text) is None:
            raise HTTPException(status_code=422, detail="Empty question or no analysis yet")
        request.app.state.claude_worker.wake()
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post("/opportunities/{opportunity_id}/rejected", response_class=HTMLResponse)
    def set_rejected(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
        source: Annotated[str, Form(alias="from")] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        # Rejection is per company: redraw this row and its company's other rows only (#138).
        opportunity = _get_opportunity(session, opportunity_id)
        toggle_manual_rejection(session, opportunity)
        session.commit()
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    @app.post("/opportunities/{opportunity_id}/notes", response_class=HTMLResponse)
    def add_opportunity_note(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        action: Annotated[str, Form()],
        kind: Annotated[NoteKind, Form()] = NoteKind.NOTE,
        detail: Annotated[str, Form()] = "",
        comment: Annotated[str, Form()] = "",
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
        source: Annotated[str, Form(alias="from")] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        # Penalties and history are per company, so the whole queue is re-rendered.
        opportunity = _get_opportunity(session, opportunity_id)
        try:
            if action == "ignore":
                ignore_with_reason(session, opportunity, kind, detail, comment)
            elif action == "note":
                add_note(session, opportunity, NoteKind.NOTE, comment=comment)
            else:
                raise HTTPException(status_code=422, detail="Unknown action")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if source == "page":  # the vacancy page: show the new state
            context = {"o": opportunity, "states": _manual_states()}
            return templates.TemplateResponse(request, "_page_state.html", context)
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    @app.post("/notes/{note_id}/delete", response_class=HTMLResponse)
    def remove_note(
        request: Request,
        note_id: int,
        session: SessionDep,
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        note = session.get(OpportunityNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        opportunity = _get_opportunity(session, note.opportunity_id)
        delete_note(session, note_id)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    def _after_manual(
        request: Request,
        session: Session,
        opportunity: Opportunity,
        source: str,
        tab: str,
        filters: Filters,
    ) -> HTMLResponse:
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        # The vacancy page: reload it, the history and the state changed.
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    @app.post("/opportunities/{opportunity_id}/events", response_class=HTMLResponse)
    def add_opportunity_event(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        kind: Annotated[str, Form()],
        at: Annotated[str, Form()] = "",
        comment: Annotated[str, Form()] = "",
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
        source: Annotated[str, Form(alias="from")] = "",
    ) -> HTMLResponse:
        # An event that is not in the mail: a call, an interview, an offer (#182).
        filters = Filters(country, company, title, q, desc, per, sort)
        opportunity = _get_opportunity(session, opportunity_id)
        try:
            day = datetime.strptime(at, "%Y-%m-%d") if at else datetime.now()
            when = day.replace(hour=12).astimezone(UTC)  # the date you picked, local noon
            manual.add_event(session, opportunity, kind, when, comment)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return _after_manual(request, session, opportunity, source, tab, filters)

    @app.post("/history/{kind}/{item_id}/delete", response_class=HTMLResponse)
    def remove_history_item(
        request: Request,
        kind: str,
        item_id: int,
        session: SessionDep,
        opportunity: Annotated[int, Form()],
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        desc: Annotated[str, Form()] = "",
        per: Annotated[str, Form()] = "",
        sort: Annotated[str, Form()] = "",
        source: Annotated[str, Form(alias="from")] = "",
    ) -> HTMLResponse:
        # Any record of the company history, from the vacancy you are looking at (#225).
        filters = Filters(country, company, title, q, desc, per, sort)
        shown = _get_opportunity(session, opportunity)
        if not delete_history_item(session, kind, item_id):
            raise HTTPException(status_code=404, detail="No such history record")
        return _after_manual(request, session, shown, source, tab, filters)

    @app.post("/opportunities", response_class=HTMLResponse)
    def add_vacancy(
        request: Request,
        session: SessionDep,
        title: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        link: Annotated[str, Form()] = "",
        location: Annotated[str, Form()] = "",
        state: Annotated[str, Form()] = "new",
        comment: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        # A vacancy found outside the sources (#182).
        try:
            opportunity = manual.add_vacancy(
                session,
                title=title,
                company=company,
                link=link,
                location=location,
                state=OpportunityState(state),
                comment=comment,
            )
        except ValueError as e:
            if request.headers.get("HX-Request"):  # show it in the dialog
                return HTMLResponse(f'<p class="neg mb-2">{escape(str(e))}</p>')
            raise HTTPException(status_code=422, detail=str(e)) from e
        url = f"/opportunities/{opportunity.id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse("", headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, session: SessionDep) -> HTMLResponse:
        context = _settings_context(session, settings)
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(
        request: Request,
        session: SessionDep,
        hybrid: Annotated[str | None, Form()] = None,
        applied: Annotated[str | None, Form()] = None,
        ignored: Annotated[str | None, Form()] = None,
        rejected: Annotated[str | None, Form()] = None,
        no_python: Annotated[str | None, Form()] = None,
        ai_location: Annotated[str | None, Form()] = None,
        language: Annotated[str | None, Form()] = None,
        unavailable: Annotated[str | None, Form()] = None,
        days: Annotated[int, Form(ge=1, le=3650)] = 90,
        countries: Annotated[str, Form()] = "",
        claude_any: Annotated[str | None, Form()] = None,
        claude_kinds: Annotated[list[str] | None, Form()] = None,
    ) -> HTMLResponse:
        rules = Rules(
            hybrid=hybrid is not None,
            applied=applied is not None,
            ignored=ignored is not None,
            rejected=rejected is not None,
            no_python=no_python is not None,
            ai_location=ai_location is not None,
            language=language is not None,
            unavailable=unavailable is not None,
            days=days,
            countries=parse_countries(countries),
            claude_any=claude_any is not None,
            claude_kinds=parse_claude_kinds(claude_kinds),
        )
        save_rules(session, rules)
        context = _settings_context(session, settings)
        context["message"] = "Saved. New vacancies follow these rules."
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/claude/analyze", response_class=HTMLResponse)
    def claude_analyze(
        request: Request, session: SessionDep, ids: Annotated[list[int] | None, Form()] = None
    ) -> HTMLResponse:
        added = claude.enqueue(session, ids or [])
        skipped = claude.without_description(session, ids or [])
        request.app.state.claude_worker.wake()
        context = {"skipped": skipped} | {
            "claude_status": claude.status(session),
            "added": added,
            "worker_off": not request.app.state.claude_worker_on,
        }
        return templates.TemplateResponse(request, "_claude_status.html", context)

    @app.get("/claude/status", response_class=HTMLResponse)
    def claude_status(request: Request, session: SessionDep) -> HTMLResponse:
        context = {
            "claude_status": claude.status(session),
            "added": None,
            "worker_off": not request.app.state.claude_worker_on,
        }
        return templates.TemplateResponse(request, "_claude_status.html", context)

    @app.post("/settings/claude", response_class=HTMLResponse)
    def save_claude_settings(
        request: Request,
        session: SessionDep,
        model: Annotated[str, Form()] = "sonnet",
        daily_cap: Annotated[int, Form(ge=0, le=500)] = 30,
        language: Annotated[str, Form()] = "Russian",
        parallel: Annotated[int, Form(ge=1, le=claude.MAX_PARALLEL)] = 4,
        chat_web: Annotated[str | None, Form()] = None,
        auto_analyze: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        if model not in claude.MODELS or language not in claude.LANGUAGES:
            raise HTTPException(status_code=422, detail="Unknown model or language")
        value = ClaudeSettings(
            model, daily_cap, language, parallel, chat_web is not None, auto_analyze is not None
        )
        claude.save_claude_settings(session, value)
        request.app.state.claude_worker.wake()  # a higher number starts more at once
        context = _settings_context(session, settings) | {"message": "Claude settings saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.get("/skills", response_class=HTMLResponse)
    def skills_page(
        request: Request,
        session: SessionDep,
        days: int = 0,
        scope: str = "open",
        sort: str = "unlock",
        dir: str = "desc",
    ) -> HTMLResponse:
        everything = scope == "all"
        report = build_report(
            session, load_profile(settings.profile), days=days or None, everything=everything
        )
        report.rows = sort_rows(report.rows, sort, dir)
        context = {"report": report, "days": days, "scope": scope, "message": ""}
        context |= {"sort": sort if sort in SORTS else "unlock", "dir": dir}
        return templates.TemplateResponse(request, "skills.html", context)

    @app.post("/skills/level", response_class=HTMLResponse)
    def skills_level(
        request: Request,
        session: SessionDep,
        name: Annotated[str, Form()],
        level: Annotated[str, Form()],
        days: Annotated[int, Form()] = 0,
        scope: Annotated[str, Form()] = "open",
    ) -> HTMLResponse:
        if level not in ("known", "learnable", "blockers") or not name.strip():
            raise HTTPException(status_code=422, detail="Unknown level")
        profile = load_profile(settings.profile) or Profile()
        lists = {k: [x for x in getattr(profile, k) if x.lower() != name.strip().lower()]
                 for k in ("known", "learnable", "blockers")}  # fmt: skip
        lists[level].append(name.strip())
        save_profile(settings.profile, replace(profile, **lists))
        report = build_report(
            session, load_profile(settings.profile), days=days or None, everything=scope == "all"
        )
        message = f"{name.strip()} is now {level.rstrip('s')} in your profile."
        context = {"report": report, "days": days, "scope": scope, "message": message}
        context |= {"sort": "unlock", "dir": "desc"}
        return templates.TemplateResponse(request, "skills.html", context)

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request, session: SessionDep) -> HTMLResponse:
        return _memory_response(request, session)

    @app.post("/memory", response_class=HTMLResponse)
    def save_memory(
        request: Request,
        session: SessionDep,
        kind: Annotated[str, Form()],
        name: Annotated[str, Form(max_length=64)],
        description: Annotated[str, Form(max_length=255)] = "",
        body: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        saved = claude_memory.save(session, kind, name, description, body, by="you")
        if saved is None:
            raise HTTPException(status_code=422, detail="Unknown kind or empty name")
        return _memory_response(request, session, f"Saved {saved}.")

    @app.post("/memory/{name}/delete", response_class=HTMLResponse)
    def delete_memory(request: Request, session: SessionDep, name: str) -> HTMLResponse:
        claude_memory.delete(session, name, by="you")
        return _memory_response(request, session, f"Deleted {name}.")

    def _memory_response(request: Request, session: Session, message: str = "") -> HTMLResponse:
        context = {
            "entries": claude_memory.entries(session),
            "changes": claude_memory.changes(session),
            "kinds": claude_memory.KINDS,
            "max_body": claude_memory.MAX_BODY,
            "message": message,
        }
        return templates.TemplateResponse(request, "memory.html", context)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request) -> HTMLResponse:
        profile = load_profile(settings.profile) or Profile()
        context = {"profile": asdict(profile), "lists": LISTS, "path": settings.profile}
        context["message"] = None
        return templates.TemplateResponse(request, "profile.html", context)

    @app.post("/profile", response_class=HTMLResponse)
    def save_profile_page(
        request: Request,
        session: SessionDep,
        known: Annotated[str, Form()] = "",
        learnable: Annotated[str, Form()] = "",
        blockers: Annotated[str, Form()] = "",
        rules: Annotated[str, Form()] = "",
        home: Annotated[str, Form()] = "",
        home_regions: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        def lines(text: str) -> list[str]:
            return [line.strip() for line in text.splitlines() if line.strip()]

        profile = Profile(
            lines(known),
            lines(learnable),
            lines(blockers),
            lines(rules),
            home=home.strip(),
            home_regions=lines(home_regions),
        )
        save_profile(settings.profile, profile)
        asked = recheck_stack(session)
        message = "Saved." + (
            f" The stack of {asked} vacancies in To review is checked again at the next Sync."
            if asked
            else ""
        )
        context = {"profile": asdict(profile), "lists": LISTS, "path": settings.profile}
        context["message"] = message
        return templates.TemplateResponse(request, "profile.html", context)

    @app.post("/settings/apply", response_class=HTMLResponse)
    def apply_settings(request: Request, session: SessionDep) -> HTMLResponse:
        moved = apply_to_review(session)
        word = "vacancy" if moved == 1 else "vacancies"
        context = _settings_context(session, settings)
        context["message"] = f"{moved} {word} auto-ignored from To review."
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/sync", response_class=HTMLResponse)
    def start_sync(request: Request) -> HTMLResponse:
        sync_job.start(sync_runner)  # a second click just shows the running one
        return templates.TemplateResponse(request, "_sync_status.html", {"job": sync_job})

    @app.get("/sync/status", response_class=HTMLResponse)
    def sync_status(request: Request, polled: bool = False) -> HTMLResponse:
        response = templates.TemplateResponse(request, "_sync_status.html", {"job": sync_job})
        return _redraw_when_over(response, polled, sync_job.state, saved=True)

    def descriptions_status(request: Request, off: bool = False) -> HTMLResponse:
        context = {"job": description_job, "off": off}
        return templates.TemplateResponse(request, "_descriptions_status.html", context)

    @app.post("/descriptions", response_class=HTMLResponse)
    def start_description_job(request: Request) -> HTMLResponse:
        if not settings.linkedin_public_search:
            return descriptions_status(request, off=True)
        start_descriptions()  # a second click just shows the running one
        return descriptions_status(request)

    @app.post("/descriptions/stop", response_class=HTMLResponse)
    def stop_description_job(request: Request) -> HTMLResponse:
        description_job.stop()
        return descriptions_status(request)

    @app.get("/descriptions/status", response_class=HTMLResponse)
    def description_job_status(request: Request, polled: bool = False) -> HTMLResponse:
        job = description_job
        return _redraw_when_over(descriptions_status(request), polled, job.state, job.saved > 0)

    def chrome_status(request: Request, off: bool = False, busy: bool = False) -> HTMLResponse:
        context = {"job": chrome_job, "off": off, "busy": busy}
        return templates.TemplateResponse(request, "_chrome_status.html", context)

    @app.post("/chrome-fill", response_class=HTMLResponse)
    def start_chrome_fill(request: Request) -> HTMLResponse:
        if not settings.linkedin_chrome:
            return chrome_status(request, off=True)
        if description_job.state == "running":
            return chrome_status(request, busy=True)
        chrome_job.start(chrome_runner)  # a second click just shows the running one
        return chrome_status(request)

    @app.post("/chrome-fill/stop", response_class=HTMLResponse)
    def stop_chrome_fill(request: Request) -> HTMLResponse:
        chrome_job.stop()
        return chrome_status(request)

    @app.get("/chrome-fill/status", response_class=HTMLResponse)
    def chrome_fill_status(request: Request, polled: bool = False) -> HTMLResponse:
        job = chrome_job
        return _redraw_when_over(chrome_status(request), polled, job.state, job.saved > 0)

    @app.get("/linkedin/log", response_class=HTMLResponse)
    def linkedin_log(request: Request, session: SessionDep, days: int = 7) -> HTMLResponse:
        rows, summary = _fetch_log(session, days)
        context = {"rows": rows, "summary": summary, "days": days}
        return templates.TemplateResponse(request, "linkedin_log.html", context)

    @app.post("/settings/linkedin", response_class=HTMLResponse)
    def save_linkedin_settings(
        request: Request,
        session: SessionDep,
        pause_from: Annotated[float, Form(ge=0, le=600)] = 10,
        pause_to: Annotated[float, Form(ge=0, le=600)] = 40,
    ) -> HTMLResponse:
        if pause_from > pause_to:
            raise HTTPException(status_code=422, detail="From must not be more than To")
        save_pause(session, PauseSettings(pause_from, pause_to))
        context = _settings_context(session, settings) | {"message": "LinkedIn pause saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings/chrome-fill", response_class=HTMLResponse)
    def save_chrome_fill_settings(
        request: Request,
        session: SessionDep,
        chrome_from: Annotated[float, Form(ge=0, le=600)] = 2,
        chrome_to: Annotated[float, Form(ge=0, le=600)] = 4,
    ) -> HTMLResponse:
        if chrome_from > chrome_to:
            raise HTTPException(status_code=422, detail="From must not be more than To")
        save_chrome_pause(session, PauseSettings(chrome_from, chrome_to))
        context = _settings_context(session, settings) | {"message": "Chrome fill pause saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings/capture", response_class=HTMLResponse)
    def save_capture_settings(
        request: Request,
        session: SessionDep,
        close_after: Annotated[float, Form(ge=0, le=60)] = CLOSE_AFTER,
    ) -> HTMLResponse:
        save_close_after(session, close_after)
        context = _settings_context(session, settings) | {"message": "Capture setting saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.get("/capture", response_class=HTMLResponse)
    def capture_page(request: Request, session: SessionDep) -> HTMLResponse:
        base = str(request.base_url)
        context = {
            "bookmarklet": bookmarklet(base),
            "debug_bookmarklet": bookmarklet(base, "bookmarklet_debug"),
            "close_ms": round(load_close_after(session) * 1000),
        }
        return templates.TemplateResponse(request, "capture.html", context)

    @app.get("/capture/debug", response_class=HTMLResponse)
    def capture_debug_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "capture_debug.html", {})

    @app.post("/api/capture")
    def capture(session: SessionDep, payload: Annotated[dict, Body()]) -> dict:
        _save_debug(settings.capture_debug_dir / "last_payload.json", json.dumps(payload, indent=1))
        return import_capture(session, payload).as_dict()

    @app.post("/api/capture/debug")
    def capture_debug(payload: Annotated[dict, Body()]) -> dict[str, str | int]:
        html = str(payload.get("html") or "")
        name = f"{datetime.now(UTC):%Y%m%d-%H%M%S}.html"
        path = settings.capture_debug_dir / name
        _save_debug(path, f"<!-- {payload.get('source_url', '')} -->\n{html}")
        return {"file": str(path), "size": len(html)}

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request, session: SessionDep, company: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "applications.html", _applications_context(session, company)
        )

    @app.post("/applications/confirm-all", response_class=HTMLResponse)
    def applications_confirm_all(
        request: Request, session: SessionDep, company: Annotated[str, Form()] = ""
    ) -> HTMLResponse:
        apps.confirm_all(session, company)  # with a filter: only the suggestions shown
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session, company)
        )

    @app.post("/applications/{app_id}/{action}", response_class=HTMLResponse)
    def applications_review(
        request: Request,
        app_id: int,
        action: str,
        session: SessionDep,
        company: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        handlers = {"confirm": apps.confirm, "dismiss": apps.dismiss}
        if action not in handlers:
            raise HTTPException(status_code=404, detail="Unknown action")
        if handlers[action](session, app_id) is None:
            raise HTTPException(status_code=404, detail="Application not found")
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session, company)
        )

    return app


def _save_debug(path: Path, text: str) -> None:
    # Local diagnostics only: data/ is git-ignored and may hold page content.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def _applications_context(session: Session, company: str = "") -> dict:
    rows = session.execute(
        select(Application, EmailMessage.gmail_thread_id)
        .outerjoin(EmailMessage, Application.email_message_id == EmailMessage.id)
        .options(joinedload(Application.company))
        .where(Application.review != ApplicationReview.DISMISSED)
        .order_by(Application.applied_at.desc())
    ).all()
    needle = company.strip().casefold()
    items = [
        {"a": a, "status": apps.application_status(session, a), "gmail": _gmail_link(thread)}
        for a, thread in rows
        if needle in a.company.name.casefold()
    ]
    suggested = [i for i in items if i["a"].review is ApplicationReview.SUGGESTED]
    confirmed = [i for i in items if i["a"].review is ApplicationReview.CONFIRMED]
    return {
        "suggested": suggested,
        "confirmed": confirmed,
        "has_any": bool(items),
        "has_rows": bool(rows),
        "company": company,
    }


def _load_opportunities(session: Session, states: list[OpportunityState] | None = None):
    query = select(Opportunity).options(
        joinedload(Opportunity.company),
        selectinload(Opportunity.source_records).selectinload(SourceRecord.snapshots),
    )
    if states is not None:
        query = query.where(Opportunity.state.in_(states))
    return session.scalars(query).all()


def _get_opportunity(session: Session, opportunity_id: int) -> Opportunity:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opportunity


def _panel_response(
    request: Request, session: Session, opportunity: Opportunity, tab: str, filters: Filters
) -> HTMLResponse:
    """The panel redrawn for this vacancy, and the list items of its company out of band
    (updated, or deleted when they left the tab), and the tab counts (#158)."""
    session.refresh(opportunity)
    states = TABS[tab][1]
    rejected = latest_rejections(session)

    def visible(o: Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = _row_context(session, opportunity, rejected) | _claude_context(
        session, opportunity.id
    )
    context |= _tabs_context(session, tab, filters) | {"mode": "panel"}
    company = session.scalars(
        select(Opportunity).where(Opportunity.company_id == opportunity.company_id)
    ).all()
    context["items_shown"] = [_row_context(session, o, rejected) for o in company if visible(o)]
    context["items_gone"] = [o.id for o in company if not visible(o)]
    return templates.TemplateResponse(request, "_panel_and_items.html", context)


def _company_rows(
    request: Request, session: Session, opportunity: Opportunity, tab: str, filters: Filters
) -> HTMLResponse:
    """This row (gone when it leaves the tab), the company's other rows out of band, and the
    tab counts: history and penalties are per company, the queue is not re-rendered (#138)."""
    session.refresh(opportunity)
    states = TABS[tab][1]
    rejected = latest_rejections(session)

    def visible(o: Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = _row_context(session, opportunity, rejected) | _tabs_context(session, tab, filters)
    context["show_row"] = visible(opportunity)
    others = session.scalars(
        select(Opportunity).where(
            Opportunity.company_id == opportunity.company_id, Opportunity.id != opportunity.id
        )
    ).all()
    context["company_rows"] = [_row_context(session, o, rejected) for o in others if visible(o)]
    return templates.TemplateResponse(request, "_row_and_tabs.html", context)


def _claude_context(session: Session, opportunity_id: int) -> dict:
    current = claude.latest(session, opportunity_id)
    chat_pending = claude.chat_pending(session, opportunity_id)
    return {
        "opportunity_id": opportunity_id,
        "claude": current,
        "messages": claude.messages(session, opportunity_id),
        "chat_ready": claude.latest_done(session, opportunity_id) is not None,
        "quick_prompts": quick_prompts(_profile(session)),
        "polling": chat_pending or (current is not None and current.state in claude.PENDING),
    }


CAPTURE_KEY = "capture"
CLOSE_AFTER = 1.5  # seconds the capture tab stays open after a good result (#221)


def load_close_after(session: Session) -> float:
    row = session.get(AppSetting, CAPTURE_KEY)
    value = row.value.get("close_after") if row and isinstance(row.value, dict) else None
    return float(value) if isinstance(value, int | float) else CLOSE_AFTER


def save_close_after(session: Session, seconds: float) -> None:
    row = session.get(AppSetting, CAPTURE_KEY) or AppSetting(key=CAPTURE_KEY)
    row.value = {"close_after": seconds}
    session.add(row)
    session.commit()


def _settings_context(session: Session, settings: Settings) -> dict:
    return {
        "rules": load_rules(session),
        "claude_kinds": CLAUDE_KINDS,
        "claude": claude.load_claude_settings(session),
        "pause": load_pause(session),
        "chrome_pause": load_chrome_pause(session),
        "chrome_on": settings.linkedin_chrome,
        "close_after": load_close_after(session),
        "models": claude.MODELS,
        "languages": claude.LANGUAGES,
        "cv_path": settings.cv_path,
        "message": None,
    }


@dataclass
class LogDay:
    day: str
    requests: int = 0
    saved: int = 0
    errors: int = 0
    stops: int = 0
    blocked_at: datetime | None = None
    blocked_reason: str | None = None


def _fetch_log(session: Session, days: int) -> tuple[list[dict], list[LogDay]]:
    """LinkedIn guest requests of the last ``days`` days, newest first, and one line per
    local day (#205)."""
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 90)))
    fetches = session.scalars(
        select(LinkedInFetch)
        .where(LinkedInFetch.at >= since.replace(tzinfo=None))
        .order_by(LinkedInFetch.at.desc(), LinkedInFetch.id.desc())
    ).all()
    # Requests logged before #205 have only the LinkedIn job id.
    old = {f.job_id for f in fetches if not f.opportunity_id and f.job_id}
    by_job = dict(
        session.execute(
            select(SourceRecord.external_id, SourceRecord.opportunity_id).where(
                SourceRecord.source_type.in_(LINKEDIN_SOURCES), SourceRecord.external_id.in_(old)
            )
        ).all()
    )
    vacancy_of = {f.id: f.opportunity_id or by_job.get(f.job_id) for f in fetches}
    ids = {i for i in vacancy_of.values() if i}
    query = select(Opportunity).where(Opportunity.id.in_(ids))
    titles = {o.id: o for o in session.scalars(query.options(joinedload(Opportunity.company)))}
    rows, by_day = [], {}
    for f in fetches:
        at = _utc(f.at)
        rows.append({"f": f, "at": at, "o": titles.get(vacancy_of[f.id])})
        day = by_day.setdefault(local_time(at, "%d.%m"), LogDay(local_time(at, "%d.%m")))
        if f.status == "stopped":
            day.stops += 1
            continue
        day.requests += 1
        day.saved += f.status == "ok" and f.kind == "description"
        day.errors += f.status == "error"
        if f.status == "blocked" and day.blocked_at is None:
            day.blocked_at, day.blocked_reason = (
                at,
                f.detail or (f"HTTP {f.http_status}" if f.http_status else None),
            )
    return rows, list(by_day.values())


def _first_gap(analysis, profile: Profile | None) -> str:
    """The first required technology not known, to prefill the Stack chip."""
    for item in (analysis.stack or []) if analysis else []:
        if item.get("status") == "required" and (
            not profile or profile.level(item["skill"]) != "known"
        ):
            return item["skill"]
    return ""


def _profile(session: Session) -> Profile | None:
    settings = session.info.get("settings")
    return current_profile(settings.profile) if settings else None


def _score(
    session: Session, opportunity: Opportunity, rejected_at: datetime | None
) -> tuple[float | None, list, str | None]:
    """The score shown and sorted by, its extra reasons, and the description used."""
    eff_score = effective_score(opportunity.score, rejected=rejected_at is not None)
    extra_reasons = []
    description = latest_description(opportunity)
    effects = (
        location_penalty(session, opportunity),
        language_risk(opportunity.title, description),
        python_risk(opportunity.title, description),
        *ai_risks(latest_analysis(opportunity), _profile(session)),
    )
    for penalty in effects:
        if penalty is None:
            continue
        points, reason = penalty
        extra_reasons.append(reason)
        if eff_score is not None:
            eff_score = round(max(0.0, eff_score + points), 1)
    return eff_score, extra_reasons, description


def _row_context(session: Session, opportunity: Opportunity, rejected: dict | None = None) -> dict:
    if rejected is None:
        rejected = latest_rejections(session)
    rejected_at = rejected.get(opportunity.company_id)
    history = company_history(session, opportunity.company_id)
    eff_score, extra_reasons, description = _score(session, opportunity, rejected_at)
    python_check = check_python(description) if description else None
    others = other_vacancies(session, opportunity.company_id, opportunity.id)
    applied = [e.at for e in history if e.kind == "applied"]
    applied += [at for o, at in others if o.state is OpportunityState.APPLIED]
    return {
        "history": history[:HISTORY_SHOWN],
        "history_count": _visible_history_count(history),
        "others": others,
        "applied_before": max(applied) if applied else None,
        "o": opportunity,
        "states": _manual_states(),
        "rejected_at": rejected_at,
        "eff_score": eff_score,
        "extra_reasons": extra_reasons,
        "python_quote": python_check.quote if python_check else "",
        "description": description or "",
        "claude": claude.latest(session, opportunity.id),
        "chat_count": len(claude.messages(session, opportunity.id)),
        "suggestions": suggest(
            opportunity,
            description=description,
            analysis=latest_analysis(opportunity),
            profile=_profile(session),
            claude=claude.latest(session, opportunity.id),
        ),
        "stack_hint": _first_gap(latest_analysis(opportunity), _profile(session)),
        "country": country_from_location(opportunity.location) or "",
        "reason_kinds": [k for k in NoteKind if k is not NoteKind.NOTE],
    }


def _tabs_context(session: Session, tab: str, filters: Filters | None = None) -> dict:
    filters = filters or Filters()
    counts: dict[OpportunityState, int] = {}
    rows = session.execute(
        select(
            Opportunity.id, Opportunity.state, Opportunity.location, Company.name, Opportunity.title
        ).join(Opportunity.company)
    ).all()
    described = set(
        session.scalars(select(SourceRecord.opportunity_id).join(JobSnapshot).distinct())
    )
    # Tab counts ignore the description switch; the switch counts the current tab (#186).
    plain = filters.with_desc("")
    in_tab = TABS[tab][1]
    desc_counts = {"": 0, "none": 0, "has": 0}
    for oid, state, location, company, title in rows:
        if plain.matches(location, company, title):
            counts[state] = counts.get(state, 0) + 1
            if in_tab is None or state in in_tab:
                desc_counts[""] += 1
                desc_counts["has" if oid in described else "none"] += 1
    tabs = [
        (
            key,
            label,
            sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states),
            filters.href(key),
        )
        for key, (label, states) in TABS.items()
    ]
    return {
        "tab": tab,
        "tabs": tabs,
        "total": len(rows),
        "filters": filters,
        "desc_counts": desc_counts,
    }


def _queue_context(
    session: Session, tab: str = DEFAULT_TAB, filters: Filters | None = None, page: int = 1
) -> dict:
    filters = filters or Filters()
    rejected = latest_rejections(session)
    opportunities = [
        o for o in _load_opportunities(session, TABS[tab][1]) if filters.matches_opportunity(o)
    ]
    # Sort by the score alone; the full row (history, Claude, hints) only for the page (#180).
    keyed = [
        {"o": o, "rejected_at": rejected.get(o.company_id)}
        | {"eff_score": _score(session, o, rejected.get(o.company_id))[0]}
        for o in opportunities
    ]
    if filters.sort == "recent":  # your last action first (#199)
        actions = last_actions(session)
        for k in keyed:
            k["last_action"] = actions.get(k["o"].id)
        # Arrival is not your action: untouched vacancies come after, newest first.
        keyed.sort(
            key=lambda k: (
                k["last_action"] is not None,
                k["last_action"][0] if k["last_action"] else _utc(k["o"].created_at),
            ),
            reverse=True,
        )
    elif filters.sort == "claude":  # Claude's match % first, unanalysed at the bottom (#207)
        matches = claude.latest_matches(session)
        keyed.sort(key=_queue_order)  # equal % keep the score order
        keyed.sort(key=lambda k: -matches.get(k["o"].id, -1))
    else:
        keyed.sort(key=_queue_order)
    per = filters.per_page
    pages = max(1, -(-len(keyed) // per))
    page = min(max(1, page), pages)
    shown = keyed[(page - 1) * per : page * per]
    rows = [
        _row_context(session, k["o"], rejected) | {"last_action": k.get("last_action")}
        for k in shown
    ]
    context = _tabs_context(session, tab, filters)
    base = filters.href(tab)
    pager = {
        "page": page,
        "pages": pages,
        "first": (page - 1) * per + 1 if keyed else 0,
        "last": (page - 1) * per + len(shown),
        "count": len(keyed),
        "prev": f"{base}&page={page - 1}" if page > 1 else None,
        "next": f"{base}&page={page + 1}" if page < pages else None,
        "per": per,
        "per_options": PER_PAGE,
    }
    return context | {
        "pager": pager,
        "rows": rows,
        "has_any": context["total"] > 0,
        "states": _manual_states(),
        "claude_status": claude.status(session),
        "added": None,
    }


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _visible_history_count(history: list) -> int:
    """How many contacts the "contacted" badge counts: the Chrome fill's marks are not (#272)."""
    return sum(e.kind not in FILL_EVENT_KINDS for e in history)


def last_actions(session: Session) -> dict[int, tuple[datetime, str]]:
    """Per vacancy: when you last did something with it, and what (#199)."""
    found: dict[int, tuple[datetime, str]] = {}

    def keep(oid: int | None, at: datetime | None, what: str) -> None:
        if oid is None or at is None:
            return
        at = _utc(at)
        if oid not in found or at > found[oid][0]:
            found[oid] = (at, what)

    for oid, at, state in session.execute(
        select(Opportunity.id, Opportunity.state_changed_at, Opportunity.state)
    ):
        keep(oid, at, state.value.replace("_", " "))
    for oid, at in session.execute(select(Application.opportunity_id, Application.applied_at)):
        keep(oid, at, "applied")
    for oid, at in session.execute(select(Rejection.opportunity_id, Rejection.rejected_at)):
        keep(oid, at, "rejected")
    for oid, at, kind in session.execute(
        select(OpportunityNote.opportunity_id, OpportunityNote.created_at, OpportunityNote.kind)
    ):
        keep(oid, at, "note" if kind is NoteKind.NOTE else "ignored")
    for oid, at, kind in session.execute(
        select(CompanyEvent.opportunity_id, CompanyEvent.at, CompanyEvent.kind).where(
            CompanyEvent.kind.not_in(FILL_EVENT_KINDS)
        )
    ):
        keep(oid, at, kind.replace("_", " "))
    for oid, at in session.execute(
        select(ClaudeMessage.opportunity_id, ClaudeMessage.created_at).where(
            ClaudeMessage.role == "user"
        )
    ):
        keep(oid, at, "chat")
    return found


def _queue_order(row: dict) -> tuple:
    o = row["o"]
    score = row["eff_score"] if row["eff_score"] is not None else -1
    # Rejected companies sink to the very bottom, but stay visible.
    return (
        row["rejected_at"] is not None,
        _STATE_ORDER.index(o.state),
        -score,
        -o.created_at.timestamp(),
    )


def _manual_states() -> list[OpportunityState]:
    automatic = (OpportunityState.NEW, OpportunityState.AUTO_IGNORE)
    return [s for s in OpportunityState if s not in automatic]
