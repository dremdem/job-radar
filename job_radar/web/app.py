"""The web application: the review queue and every page around it.

``create_app`` builds the FastAPI app with its database session, background jobs and
Claude workers. Pages: the queue (``/``, ``/opportunities``) with a vacancy panel, notes,
history and chat; Applications, Skills, Memory, Profile and Settings; the LinkedIn request
log. Buttons post to small endpoints (``/sync``, ``/descriptions``, ``/chrome-fill``,
``/claude/analyze``) that start background work and report its status for htmx to poll.
``/api/capture`` receives pages from the bookmarklet. See docs/queue.md and
docs/settings.md.

Usage:
    import job_radar.web.app as web_app
"""

import collections.abc as cabc
import contextlib
import dataclasses
import datetime as dt
import json
import pathlib
import re
import typing as t
import urllib.parse as urllib_parse

import fastapi
import fastapi.responses as fastapi_responses
import fastapi.templating as fastapi_templating
import markupsafe
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.ai.analysis as ai_analysis
import job_radar.applications as applications
import job_radar.auto_ignore as auto_ignore
import job_radar.claude.analysis as claude_analysis
import job_radar.claude.memory as claude_memory
import job_radar.claude.prompt as claude_prompt
import job_radar.claude.runner as jr_claude_runner
import job_radar.config as config
import job_radar.contacts as contacts
import job_radar.db as db
import job_radar.linkedin.alerts as linkedin_alerts
import job_radar.linkedin.capture as linkedin_capture
import job_radar.linkedin.chrome as linkedin_chrome
import job_radar.linkedin.public as linkedin_public
import job_radar.manual as manual
import job_radar.models as models
import job_radar.notes as notes
import job_radar.pipeline as pipeline
import job_radar.profile.stack as profile_stack
import job_radar.rejections as rejections
import job_radar.scoring.description as scoring_description
import job_radar.scoring.findings as scoring_findings
import job_radar.scoring.language as scoring_language
import job_radar.skills as skills
import job_radar.suggestions as suggestions
import job_radar.web.description_job as web_description_job
import job_radar.web.sync_job as web_sync_job

HISTORY_SHOWN = 8
# What the bookmarklet reads, shared with the Chrome fill (#247): one reader for both.
CAPTURE_READ = (pathlib.Path(__file__).parent / "capture_read.js").read_text()
BOOKMARKLETS = {
    name: (pathlib.Path(__file__).parent / f"{name}.js")
    .read_text()
    .replace("__READ__", CAPTURE_READ)
    for name in ("bookmarklet", "bookmarklet_debug")
}


def bookmarklet(base_url: str, name: str = "bookmarklet") -> str:
    """The bookmarklet as a javascript: URL; comments and indentation stripped."""
    code = re.sub(r"/\*.*?\*/", "", BOOKMARKLETS[name], flags=re.S)
    code = " ".join(line.strip() for line in code.splitlines() if line.strip())
    return "javascript:" + code.replace("__BASE__", base_url.rstrip("/"))


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


def _pause_left(until: dt.datetime | None) -> str:
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
templates.env.globals["EVENT_KINDS"] = manual.EVENT_KINDS
templates.env.globals["reason_icon"] = notes.reason_icon
templates.env.globals["today"] = lambda: dt.datetime.now().strftime("%Y-%m-%d")

# Queue order: undecided first, then the ones worth a look.
_STATE_ORDER = [
    models.OpportunityState.NEW,
    models.OpportunityState.INTERESTED,
    models.OpportunityState.APPLIED,
    models.OpportunityState.IGNORE,
    models.OpportunityState.AUTO_IGNORE,
]


# Queue tabs: key -> (label, states shown; None = all).
TABS: dict[str, tuple[str, list[models.OpportunityState] | None]] = {
    "review": ("To review", [models.OpportunityState.NEW]),
    "interested": ("Interested", [models.OpportunityState.INTERESTED]),
    "applied": ("Applied", [models.OpportunityState.APPLIED]),
    "ignored": ("Ignored", [models.OpportunityState.IGNORE]),
    "auto": ("Auto-ignored", [models.OpportunityState.AUTO_IGNORE]),
    "all": ("All", None),
}
DEFAULT_TAB = "review"
PER_PAGE = (25, 50, 100, 200)
DEFAULT_PER_PAGE = 50


@dataclasses.dataclass(frozen=True)
class Filters:
    """Queue filters: any part of the text, case-insensitive (non-ASCII too)."""

    country: str = ""
    company: str = ""
    title: str = ""
    # the inbox search box: title, company or location (#158)
    q: str = ""
    # "none" / "has": vacancies without / with a saved description (#176)
    desc: str = ""
    # vacancies per page, one of PER_PAGE; "" = the default (#180)
    per: str = ""
    # "" = score, "claude" = Claude match % (#207), "recent" = last action (#199)
    sort: str = ""

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

    def matches_opportunity(self, o: models.Opportunity) -> bool:
        described = any(r.snapshots for r in o.source_records)
        return self.matches(o.location, o.company.name, o.title, described)

    def with_desc(self, desc: str) -> "Filters":
        return dataclasses.replace(self, desc=desc)

    def with_sort(self, sort: str) -> "Filters":
        return dataclasses.replace(self, sort=sort)

    def with_per(self, per: int) -> "Filters":
        return dataclasses.replace(self, per="" if per == DEFAULT_PER_PAGE else str(per))

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
        return "/?" + urllib_parse.urlencode({"tab": tab} | {k: v for k, v in pairs if v})


def _tab(value: str | None) -> str:
    return value if value in TABS else DEFAULT_TAB


def get_session(request: fastapi.Request) -> cabc.Iterator[sa_orm.Session]:
    with request.app.state.session_factory() as session:
        # Row helpers below read per-user files (the profile) through the session.
        session.info["settings"] = request.app.state.settings
        yield session


SessionDep = t.Annotated[sa_orm.Session, fastapi.Depends(get_session)]


QUEUE_REDRAW = "jr-refresh-queue"


def _redraw_when_over(
    response: fastapi_responses.HTMLResponse, polled: bool, state: str, saved: bool
) -> fastapi_responses.HTMLResponse:
    """The poll that sees a job end, while the page watched it run, asks the queue to redraw
    its list and counters (#263). A page loaded later already shows the new state."""
    if polled and saved and state in ("done", "failed"):
        response.headers["HX-Trigger"] = QUEUE_REDRAW
    return response


def create_app(
    settings: config.Settings | None = None,
    sync_runner: web_sync_job.Runner | None = None,
    claude_runner: jr_claude_runner.Runner | None = None,
    start_worker: bool = True,
    description_runner: cabc.Callable[[web_description_job.DescriptionJob], None] | None = None,
    chrome_runner: cabc.Callable[[web_description_job.DescriptionJob], None] | None = None,
) -> fastapi.FastAPI:
    """``start_worker=False``: tests drive the Claude worker with ``run_once``."""
    settings = settings or config.get_settings()
    sync_job = web_sync_job.SyncJob()
    description_job = web_description_job.DescriptionJob()
    description_runner = description_runner or (lambda job: pipeline.description_job(settings, job))

    def start_descriptions() -> bool:
        # one LinkedIn description run at a time
        if chrome_job.state == "running":
            return False
        return description_job.start(description_runner)

    # The Chrome fill (#247): descriptions read in your own Chrome; tests pass a fake.
    chrome_job = web_description_job.DescriptionJob(blocked_note="stopped: {reason}")
    chrome_runner = chrome_runner or (lambda job: pipeline.chrome_fill_job(settings, job))

    # The Sync button runs the same pipeline as `./jr sync`, but leaves the slow LinkedIn
    # descriptions to the background job (#205); tests pass a fake.
    sync_runner = sync_runner or (
        lambda echo, on_steps: pipeline.run_sync(
            settings, echo, on_steps=on_steps, start_descriptions=start_descriptions
        )
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: fastapi.FastAPI) -> cabc.AsyncIterator[None]:
        # New code may bring new tables; the server must not wait for a CLI command.
        db.upgrade_database(settings.database_url)
        engine = db.make_engine(settings.database_url)
        app.state.session_factory = db.make_session_factory(engine)
        app.state.settings = settings
        worker = claude_analysis.Worker(
            app.state.session_factory,
            claude_runner
            or jr_claude_runner.ClaudeCli(settings.claude_command, settings.claude_workdir),
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

    app = fastapi.FastAPI(title="Job Radar", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=fastapi_responses.HTMLResponse)
    def queue(
        request: fastapi.Request,
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
    ) -> fastapi_responses.HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        return templates.TemplateResponse(
            request, "queue.html", _queue_context(session, _tab(tab), filters, page)
        )

    @app.post(
        "/opportunities/{opportunity_id}/state", response_class=fastapi_responses.HTMLResponse
    )
    def set_state(
        request: fastapi.Request,
        opportunity_id: int,
        state: t.Annotated[models.OpportunityState, fastapi.Form()],
        session: SessionDep,
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
        source: t.Annotated[str, fastapi.Form(alias="from")] = "",
    ) -> fastapi_responses.HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        tab = _tab(tab)
        opportunity = _get_opportunity(session, opportunity_id)
        opportunity.state = state
        opportunity.state_changed_at = dt.datetime.now(dt.UTC)
        # your choice replaces the rule's
        opportunity.auto_ignore_reason = None
        session.commit()
        # the vacancy page: redraw its buttons only
        if source == "page":
            context = {"o": opportunity, "states": _manual_states()}
            return templates.TemplateResponse(request, "_page_state.html", context)
        # the inbox panel (#158)
        if source == "panel":
            return _panel_response(request, session, opportunity, tab, filters)
        # Replace only this row (or remove it when it leaves the tab) and refresh the tab counts
        # out of band: re-rendering the whole queue made the page jump to the end (#70).
        states = TABS[tab][1]
        context = _row_context(session, opportunity) | _tabs_context(session, tab, filters)
        context["show_row"] = states is None or opportunity.state in states
        return templates.TemplateResponse(request, "_row_and_tabs.html", context)

    @app.post("/opportunities/{opportunity_id}/unavailable/clear")
    def clear_unavailable_mark(
        opportunity_id: int, session: SessionDep
    ) -> fastapi_responses.RedirectResponse:
        # The cross on the "unavailable" badge: the next Chrome fill may try it again (#276).
        opportunity = _get_opportunity(session, opportunity_id)
        auto_ignore.clear_unavailable(opportunity)
        session.commit()
        return fastapi_responses.RedirectResponse(
            f"/opportunities/{opportunity.id}", status_code=303
        )

    @app.get("/opportunities/{opportunity_id}/row", response_class=fastapi_responses.HTMLResponse)
    def opportunity_row(
        request: fastapi.Request,
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
    ) -> fastapi_responses.HTMLResponse:
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

    @app.get("/opportunities/{opportunity_id}/panel", response_class=fastapi_responses.HTMLResponse)
    def opportunity_panel(
        request: fastapi.Request, opportunity_id: int, session: SessionDep, tab: str = DEFAULT_TAB
    ) -> fastapi_responses.HTMLResponse:
        # The inbox's right-hand panel (#158): the vacancy page's content.
        opportunity = _get_opportunity(session, opportunity_id)
        context = _row_context(session, opportunity) | _claude_context(session, opportunity_id)
        context |= {"tab": _tab(tab), "mode": "panel"}
        return templates.TemplateResponse(request, "_detail.html", context)

    @app.get("/opportunities/{opportunity_id}", response_class=fastapi_responses.HTMLResponse)
    def opportunity_page(
        request: fastapi.Request, opportunity_id: int, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        opportunity = _get_opportunity(session, opportunity_id)
        context = _row_context(session, opportunity) | _claude_context(session, opportunity_id)
        context |= {"tab": DEFAULT_TAB, "mode": "page"}
        return templates.TemplateResponse(request, "opportunity.html", context)

    @app.get(
        "/opportunities/{opportunity_id}/claude", response_class=fastapi_responses.HTMLResponse
    )
    def opportunity_claude(
        request: fastapi.Request, opportunity_id: int, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        _get_opportunity(session, opportunity_id)
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post(
        "/opportunities/{opportunity_id}/claude", response_class=fastapi_responses.HTMLResponse
    )
    def opportunity_analyze(
        request: fastapi.Request, opportunity_id: int, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        _get_opportunity(session, opportunity_id)
        claude_analysis.enqueue(session, [opportunity_id])
        request.app.state.claude_worker.wake()
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post("/opportunities/{opportunity_id}/chat", response_class=fastapi_responses.HTMLResponse)
    def opportunity_ask(
        request: fastapi.Request,
        opportunity_id: int,
        session: SessionDep,
        text: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        _get_opportunity(session, opportunity_id)
        if claude_analysis.ask(session, opportunity_id, text) is None:
            raise fastapi.HTTPException(status_code=422, detail="Empty question or no analysis yet")
        request.app.state.claude_worker.wake()
        context = _claude_context(session, opportunity_id)
        return templates.TemplateResponse(request, "_claude_section.html", context)

    @app.post(
        "/opportunities/{opportunity_id}/rejected", response_class=fastapi_responses.HTMLResponse
    )
    def set_rejected(
        request: fastapi.Request,
        opportunity_id: int,
        session: SessionDep,
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
        source: t.Annotated[str, fastapi.Form(alias="from")] = "",
    ) -> fastapi_responses.HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        # Rejection is per company: redraw this row and its company's other rows only (#138).
        opportunity = _get_opportunity(session, opportunity_id)
        rejections.toggle_manual_rejection(session, opportunity)
        session.commit()
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    @app.post(
        "/opportunities/{opportunity_id}/notes", response_class=fastapi_responses.HTMLResponse
    )
    def add_opportunity_note(
        request: fastapi.Request,
        opportunity_id: int,
        session: SessionDep,
        action: t.Annotated[str, fastapi.Form()],
        kind: t.Annotated[models.NoteKind, fastapi.Form()] = models.NoteKind.NOTE,
        detail: t.Annotated[str, fastapi.Form()] = "",
        comment: t.Annotated[str, fastapi.Form()] = "",
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
        source: t.Annotated[str, fastapi.Form(alias="from")] = "",
    ) -> fastapi_responses.HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        # Penalties and history are per company, so the whole queue is re-rendered.
        opportunity = _get_opportunity(session, opportunity_id)
        try:
            if action == "ignore":
                notes.ignore_with_reason(session, opportunity, kind, detail, comment)
            elif action == "note":
                notes.add_note(session, opportunity, models.NoteKind.NOTE, comment=comment)
            else:
                raise fastapi.HTTPException(status_code=422, detail="Unknown action")
        except ValueError as e:
            raise fastapi.HTTPException(status_code=422, detail=str(e)) from e
        # the vacancy page: show the new state
        if source == "page":
            context = {"o": opportunity, "states": _manual_states()}
            return templates.TemplateResponse(request, "_page_state.html", context)
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    @app.post("/notes/{note_id}/delete", response_class=fastapi_responses.HTMLResponse)
    def remove_note(
        request: fastapi.Request,
        note_id: int,
        session: SessionDep,
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        filters = Filters(country, company, title, q, desc, per, sort)
        note = session.get(models.OpportunityNote, note_id)
        if note is None:
            raise fastapi.HTTPException(status_code=404, detail="Note not found")
        opportunity = _get_opportunity(session, note.opportunity_id)
        notes.delete_note(session, note_id)
        return _company_rows(request, session, opportunity, _tab(tab), filters)

    def _after_manual(
        request: fastapi.Request,
        session: sa_orm.Session,
        opportunity: models.Opportunity,
        source: str,
        tab: str,
        filters: Filters,
    ) -> fastapi_responses.HTMLResponse:
        if source == "panel":
            return _panel_response(request, session, opportunity, _tab(tab), filters)
        # The vacancy page: reload it, the history and the state changed.
        return fastapi_responses.HTMLResponse("", headers={"HX-Refresh": "true"})

    @app.post(
        "/opportunities/{opportunity_id}/events", response_class=fastapi_responses.HTMLResponse
    )
    def add_opportunity_event(
        request: fastapi.Request,
        opportunity_id: int,
        session: SessionDep,
        kind: t.Annotated[str, fastapi.Form()],
        at: t.Annotated[str, fastapi.Form()] = "",
        comment: t.Annotated[str, fastapi.Form()] = "",
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
        source: t.Annotated[str, fastapi.Form(alias="from")] = "",
    ) -> fastapi_responses.HTMLResponse:
        # An event that is not in the mail: a call, an interview, an offer (#182).
        filters = Filters(country, company, title, q, desc, per, sort)
        opportunity = _get_opportunity(session, opportunity_id)
        try:
            day = dt.datetime.strptime(at, "%Y-%m-%d") if at else dt.datetime.now()
            # the date you picked, local noon
            when = day.replace(hour=12).astimezone(dt.UTC)
            manual.add_event(session, opportunity, kind, when, comment)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=422, detail=str(e)) from e
        return _after_manual(request, session, opportunity, source, tab, filters)

    @app.post("/history/{kind}/{item_id}/delete", response_class=fastapi_responses.HTMLResponse)
    def remove_history_item(
        request: fastapi.Request,
        kind: str,
        item_id: int,
        session: SessionDep,
        opportunity: t.Annotated[int, fastapi.Form()],
        tab: t.Annotated[str, fastapi.Form()] = DEFAULT_TAB,
        country: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        title: t.Annotated[str, fastapi.Form()] = "",
        q: t.Annotated[str, fastapi.Form()] = "",
        desc: t.Annotated[str, fastapi.Form()] = "",
        per: t.Annotated[str, fastapi.Form()] = "",
        sort: t.Annotated[str, fastapi.Form()] = "",
        source: t.Annotated[str, fastapi.Form(alias="from")] = "",
    ) -> fastapi_responses.HTMLResponse:
        # Any record of the company history, from the vacancy you are looking at (#225).
        filters = Filters(country, company, title, q, desc, per, sort)
        shown = _get_opportunity(session, opportunity)
        if not contacts.delete_history_item(session, kind, item_id):
            raise fastapi.HTTPException(status_code=404, detail="No such history record")
        return _after_manual(request, session, shown, source, tab, filters)

    @app.post("/opportunities", response_class=fastapi_responses.HTMLResponse)
    def add_vacancy(
        request: fastapi.Request,
        session: SessionDep,
        title: t.Annotated[str, fastapi.Form()] = "",
        company: t.Annotated[str, fastapi.Form()] = "",
        link: t.Annotated[str, fastapi.Form()] = "",
        location: t.Annotated[str, fastapi.Form()] = "",
        state: t.Annotated[str, fastapi.Form()] = "new",
        comment: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        # A vacancy found outside the sources (#182).
        try:
            opportunity = manual.add_vacancy(
                session,
                title=title,
                company=company,
                link=link,
                location=location,
                state=models.OpportunityState(state),
                comment=comment,
            )
        except ValueError as e:
            # show it in the dialog
            if request.headers.get("HX-Request"):
                return fastapi_responses.HTMLResponse(
                    f'<p class="neg mb-2">{markupsafe.escape(str(e))}</p>'
                )
            raise fastapi.HTTPException(status_code=422, detail=str(e)) from e
        url = f"/opportunities/{opportunity.id}"
        if request.headers.get("HX-Request"):
            return fastapi_responses.HTMLResponse("", headers={"HX-Redirect": url})
        return fastapi_responses.RedirectResponse(url, status_code=303)

    @app.get("/settings", response_class=fastapi_responses.HTMLResponse)
    def settings_page(
        request: fastapi.Request, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        context = _settings_context(session, settings)
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings", response_class=fastapi_responses.HTMLResponse)
    def save_settings(
        request: fastapi.Request,
        session: SessionDep,
        hybrid: t.Annotated[str | None, fastapi.Form()] = None,
        applied: t.Annotated[str | None, fastapi.Form()] = None,
        ignored: t.Annotated[str | None, fastapi.Form()] = None,
        rejected: t.Annotated[str | None, fastapi.Form()] = None,
        no_python: t.Annotated[str | None, fastapi.Form()] = None,
        ai_location: t.Annotated[str | None, fastapi.Form()] = None,
        language: t.Annotated[str | None, fastapi.Form()] = None,
        unavailable: t.Annotated[str | None, fastapi.Form()] = None,
        days: t.Annotated[int, fastapi.Form(ge=1, le=3650)] = 90,
        countries: t.Annotated[str, fastapi.Form()] = "",
        claude_any: t.Annotated[str | None, fastapi.Form()] = None,
        claude_kinds: t.Annotated[list[str] | None, fastapi.Form()] = None,
    ) -> fastapi_responses.HTMLResponse:
        rules = auto_ignore.Rules(
            hybrid=hybrid is not None,
            applied=applied is not None,
            ignored=ignored is not None,
            rejected=rejected is not None,
            no_python=no_python is not None,
            ai_location=ai_location is not None,
            language=language is not None,
            unavailable=unavailable is not None,
            days=days,
            countries=auto_ignore.parse_countries(countries),
            claude_any=claude_any is not None,
            claude_kinds=auto_ignore.parse_claude_kinds(claude_kinds),
        )
        auto_ignore.save_rules(session, rules)
        context = _settings_context(session, settings)
        context["message"] = "Saved. New vacancies follow these rules."
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/claude/analyze", response_class=fastapi_responses.HTMLResponse)
    def claude_analyze(
        request: fastapi.Request,
        session: SessionDep,
        ids: t.Annotated[list[int] | None, fastapi.Form()] = None,
    ) -> fastapi_responses.HTMLResponse:
        added = claude_analysis.enqueue(session, ids or [])
        skipped = claude_analysis.without_description(session, ids or [])
        request.app.state.claude_worker.wake()
        context = {"skipped": skipped} | {
            "claude_status": claude_analysis.status(session),
            "added": added,
            "worker_off": not request.app.state.claude_worker_on,
        }
        return templates.TemplateResponse(request, "_claude_status.html", context)

    @app.get("/claude/status", response_class=fastapi_responses.HTMLResponse)
    def claude_status(
        request: fastapi.Request, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        context = {
            "claude_status": claude_analysis.status(session),
            "added": None,
            "worker_off": not request.app.state.claude_worker_on,
        }
        return templates.TemplateResponse(request, "_claude_status.html", context)

    @app.post("/settings/claude", response_class=fastapi_responses.HTMLResponse)
    def save_claude_settings(
        request: fastapi.Request,
        session: SessionDep,
        model: t.Annotated[str, fastapi.Form()] = "sonnet",
        daily_cap: t.Annotated[int, fastapi.Form(ge=0, le=500)] = 30,
        language: t.Annotated[str, fastapi.Form()] = "Russian",
        parallel: t.Annotated[int, fastapi.Form(ge=1, le=claude_analysis.MAX_PARALLEL)] = 4,
        chat_web: t.Annotated[str | None, fastapi.Form()] = None,
        auto_analyze: t.Annotated[str | None, fastapi.Form()] = None,
    ) -> fastapi_responses.HTMLResponse:
        if model not in claude_analysis.MODELS or language not in claude_analysis.LANGUAGES:
            raise fastapi.HTTPException(status_code=422, detail="Unknown model or language")
        value = claude_analysis.ClaudeSettings(
            model, daily_cap, language, parallel, chat_web is not None, auto_analyze is not None
        )
        claude_analysis.save_claude_settings(session, value)
        # a higher number starts more at once
        request.app.state.claude_worker.wake()
        context = _settings_context(session, settings) | {"message": "Claude settings saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.get("/skills", response_class=fastapi_responses.HTMLResponse)
    def skills_page(
        request: fastapi.Request,
        session: SessionDep,
        days: int = 0,
        scope: str = "open",
        sort: str = "unlock",
        dir: str = "desc",
    ) -> fastapi_responses.HTMLResponse:
        everything = scope == "all"
        report = skills.build_report(
            session,
            profile_stack.load_profile(settings.profile),
            days=days or None,
            everything=everything,
        )
        report.rows = skills.sort_rows(report.rows, sort, dir)
        context = {"report": report, "days": days, "scope": scope, "message": ""}
        context |= {"sort": sort if sort in skills.SORTS else "unlock", "dir": dir}
        return templates.TemplateResponse(request, "skills.html", context)

    @app.post("/skills/level", response_class=fastapi_responses.HTMLResponse)
    def skills_level(
        request: fastapi.Request,
        session: SessionDep,
        name: t.Annotated[str, fastapi.Form()],
        level: t.Annotated[str, fastapi.Form()],
        days: t.Annotated[int, fastapi.Form()] = 0,
        scope: t.Annotated[str, fastapi.Form()] = "open",
    ) -> fastapi_responses.HTMLResponse:
        if level not in ("known", "learnable", "blockers") or not name.strip():
            raise fastapi.HTTPException(status_code=422, detail="Unknown level")
        profile = profile_stack.load_profile(settings.profile) or profile_stack.Profile()
        lists = {k: [x for x in getattr(profile, k) if x.lower() != name.strip().lower()]
                 for k in ("known", "learnable", "blockers")}  # fmt: skip
        lists[level].append(name.strip())
        profile_stack.save_profile(settings.profile, dataclasses.replace(profile, **lists))
        report = skills.build_report(
            session,
            profile_stack.load_profile(settings.profile),
            days=days or None,
            everything=scope == "all",
        )
        message = f"{name.strip()} is now {level.rstrip('s')} in your profile."
        context = {"report": report, "days": days, "scope": scope, "message": message}
        context |= {"sort": "unlock", "dir": "desc"}
        return templates.TemplateResponse(request, "skills.html", context)

    @app.get("/memory", response_class=fastapi_responses.HTMLResponse)
    def memory_page(
        request: fastapi.Request, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        return _memory_response(request, session)

    @app.post("/memory", response_class=fastapi_responses.HTMLResponse)
    def save_memory(
        request: fastapi.Request,
        session: SessionDep,
        kind: t.Annotated[str, fastapi.Form()],
        name: t.Annotated[str, fastapi.Form(max_length=64)],
        description: t.Annotated[str, fastapi.Form(max_length=255)] = "",
        body: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        saved = claude_memory.save(session, kind, name, description, body, by="you")
        if saved is None:
            raise fastapi.HTTPException(status_code=422, detail="Unknown kind or empty name")
        return _memory_response(request, session, f"Saved {saved}.")

    @app.post("/memory/{name}/delete", response_class=fastapi_responses.HTMLResponse)
    def delete_memory(
        request: fastapi.Request, session: SessionDep, name: str
    ) -> fastapi_responses.HTMLResponse:
        claude_memory.delete(session, name, by="you")
        return _memory_response(request, session, f"Deleted {name}.")

    def _memory_response(
        request: fastapi.Request, session: sa_orm.Session, message: str = ""
    ) -> fastapi_responses.HTMLResponse:
        context = {
            "entries": claude_memory.entries(session),
            "changes": claude_memory.changes(session),
            "kinds": claude_memory.KINDS,
            "max_body": claude_memory.MAX_BODY,
            "message": message,
        }
        return templates.TemplateResponse(request, "memory.html", context)

    @app.get("/profile", response_class=fastapi_responses.HTMLResponse)
    def profile_page(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        profile = profile_stack.load_profile(settings.profile) or profile_stack.Profile()
        context = {
            "profile": dataclasses.asdict(profile),
            "lists": profile_stack.LISTS,
            "path": settings.profile,
        }
        context["message"] = None
        return templates.TemplateResponse(request, "profile.html", context)

    @app.post("/profile", response_class=fastapi_responses.HTMLResponse)
    def save_profile_page(
        request: fastapi.Request,
        session: SessionDep,
        known: t.Annotated[str, fastapi.Form()] = "",
        learnable: t.Annotated[str, fastapi.Form()] = "",
        blockers: t.Annotated[str, fastapi.Form()] = "",
        rules: t.Annotated[str, fastapi.Form()] = "",
        home: t.Annotated[str, fastapi.Form()] = "",
        home_regions: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        def lines(text: str) -> list[str]:
            return [line.strip() for line in text.splitlines() if line.strip()]

        profile = profile_stack.Profile(
            lines(known),
            lines(learnable),
            lines(blockers),
            lines(rules),
            home=home.strip(),
            home_regions=lines(home_regions),
        )
        profile_stack.save_profile(settings.profile, profile)
        asked = ai_analysis.recheck_stack(session)
        message = "Saved." + (
            f" The stack of {asked} vacancies in To review is checked again at the next Sync."
            if asked
            else ""
        )
        context = {
            "profile": dataclasses.asdict(profile),
            "lists": profile_stack.LISTS,
            "path": settings.profile,
        }
        context["message"] = message
        return templates.TemplateResponse(request, "profile.html", context)

    @app.post("/settings/apply", response_class=fastapi_responses.HTMLResponse)
    def apply_settings(
        request: fastapi.Request, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        moved = auto_ignore.apply_to_review(session)
        word = "vacancy" if moved == 1 else "vacancies"
        context = _settings_context(session, settings)
        context["message"] = f"{moved} {word} auto-ignored from To review."
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/sync", response_class=fastapi_responses.HTMLResponse)
    def start_sync(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        # a second click just shows the running one
        sync_job.start(sync_runner)
        return templates.TemplateResponse(request, "_sync_status.html", {"job": sync_job})

    @app.get("/sync/status", response_class=fastapi_responses.HTMLResponse)
    def sync_status(
        request: fastapi.Request, polled: bool = False
    ) -> fastapi_responses.HTMLResponse:
        response = templates.TemplateResponse(request, "_sync_status.html", {"job": sync_job})
        return _redraw_when_over(response, polled, sync_job.state, saved=True)

    def descriptions_status(
        request: fastapi.Request, off: bool = False
    ) -> fastapi_responses.HTMLResponse:
        context = {"job": description_job, "off": off}
        return templates.TemplateResponse(request, "_descriptions_status.html", context)

    @app.post("/descriptions", response_class=fastapi_responses.HTMLResponse)
    def start_description_job(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        if not pipeline.descriptions_on(settings):
            return descriptions_status(request, off=True)
        # a second click just shows the running one
        start_descriptions()
        return descriptions_status(request)

    @app.post("/descriptions/stop", response_class=fastapi_responses.HTMLResponse)
    def stop_description_job(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        description_job.stop()
        return descriptions_status(request)

    @app.get("/descriptions/status", response_class=fastapi_responses.HTMLResponse)
    def description_job_status(
        request: fastapi.Request, polled: bool = False
    ) -> fastapi_responses.HTMLResponse:
        job = description_job
        return _redraw_when_over(descriptions_status(request), polled, job.state, job.saved > 0)

    def chrome_status(
        request: fastapi.Request, off: bool = False, busy: bool = False
    ) -> fastapi_responses.HTMLResponse:
        context = {"job": chrome_job, "off": off, "busy": busy}
        return templates.TemplateResponse(request, "_chrome_status.html", context)

    @app.post("/chrome-fill", response_class=fastapi_responses.HTMLResponse)
    def start_chrome_fill(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        if not settings.linkedin_chrome:
            return chrome_status(request, off=True)
        if description_job.state == "running":
            return chrome_status(request, busy=True)
        # a second click just shows the running one
        chrome_job.start(chrome_runner)
        return chrome_status(request)

    @app.post("/chrome-fill/stop", response_class=fastapi_responses.HTMLResponse)
    def stop_chrome_fill(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        chrome_job.stop()
        return chrome_status(request)

    @app.get("/chrome-fill/status", response_class=fastapi_responses.HTMLResponse)
    def chrome_fill_status(
        request: fastapi.Request, polled: bool = False
    ) -> fastapi_responses.HTMLResponse:
        job = chrome_job
        return _redraw_when_over(chrome_status(request), polled, job.state, job.saved > 0)

    @app.get("/linkedin/log", response_class=fastapi_responses.HTMLResponse)
    def linkedin_log(
        request: fastapi.Request, session: SessionDep, days: int = 7
    ) -> fastapi_responses.HTMLResponse:
        rows, summary = _fetch_log(session, days)
        context = {"rows": rows, "summary": summary, "days": days}
        return templates.TemplateResponse(request, "linkedin_log.html", context)

    @app.post("/settings/linkedin", response_class=fastapi_responses.HTMLResponse)
    def save_linkedin_settings(
        request: fastapi.Request,
        session: SessionDep,
        pause_from: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 10,
        pause_to: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 40,
    ) -> fastapi_responses.HTMLResponse:
        if pause_from > pause_to:
            raise fastapi.HTTPException(status_code=422, detail="From must not be more than To")
        linkedin_public.save_pause(session, linkedin_public.PauseSettings(pause_from, pause_to))
        context = _settings_context(session, settings) | {"message": "LinkedIn pause saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings/chrome-fill", response_class=fastapi_responses.HTMLResponse)
    def save_chrome_fill_settings(
        request: fastapi.Request,
        session: SessionDep,
        chrome_from: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 2,
        chrome_to: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 4,
    ) -> fastapi_responses.HTMLResponse:
        if chrome_from > chrome_to:
            raise fastapi.HTTPException(status_code=422, detail="From must not be more than To")
        linkedin_chrome.save_chrome_pause(
            session, linkedin_public.PauseSettings(chrome_from, chrome_to)
        )
        context = _settings_context(session, settings) | {"message": "Chrome fill pause saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings/capture", response_class=fastapi_responses.HTMLResponse)
    def save_capture_settings(
        request: fastapi.Request,
        session: SessionDep,
        close_after: t.Annotated[float, fastapi.Form(ge=0, le=60)] = CLOSE_AFTER,
    ) -> fastapi_responses.HTMLResponse:
        save_close_after(session, close_after)
        context = _settings_context(session, settings) | {"message": "Capture setting saved."}
        return templates.TemplateResponse(request, "settings.html", context)

    @app.get("/capture", response_class=fastapi_responses.HTMLResponse)
    def capture_page(
        request: fastapi.Request, session: SessionDep
    ) -> fastapi_responses.HTMLResponse:
        base = str(request.base_url)
        context = {
            "bookmarklet": bookmarklet(base),
            "debug_bookmarklet": bookmarklet(base, "bookmarklet_debug"),
            "close_ms": round(load_close_after(session) * 1000),
        }
        return templates.TemplateResponse(request, "capture.html", context)

    @app.get("/capture/debug", response_class=fastapi_responses.HTMLResponse)
    def capture_debug_page(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
        return templates.TemplateResponse(request, "capture_debug.html", {})

    @app.post("/api/capture")
    def capture(session: SessionDep, payload: t.Annotated[dict, fastapi.Body()]) -> dict:
        _save_debug(settings.capture_debug_dir / "last_payload.json", json.dumps(payload, indent=1))
        return linkedin_capture.import_capture(session, payload).as_dict()

    @app.post("/api/capture/debug")
    def capture_debug(payload: t.Annotated[dict, fastapi.Body()]) -> dict[str, str | int]:
        html = str(payload.get("html") or "")
        name = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}.html"
        path = settings.capture_debug_dir / name
        _save_debug(path, f"<!-- {payload.get('source_url', '')} -->\n{html}")
        return {"file": str(path), "size": len(html)}

    @app.get("/applications", response_class=fastapi_responses.HTMLResponse)
    def applications_page(
        request: fastapi.Request, session: SessionDep, company: str = ""
    ) -> fastapi_responses.HTMLResponse:
        return templates.TemplateResponse(
            request, "applications.html", _applications_context(session, company)
        )

    @app.post("/applications/confirm-all", response_class=fastapi_responses.HTMLResponse)
    def applications_confirm_all(
        request: fastapi.Request,
        session: SessionDep,
        company: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        # with a filter: only the suggestions shown
        applications.confirm_all(session, company)
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session, company)
        )

    @app.post("/applications/{app_id}/{action}", response_class=fastapi_responses.HTMLResponse)
    def applications_review(
        request: fastapi.Request,
        app_id: int,
        action: str,
        session: SessionDep,
        company: t.Annotated[str, fastapi.Form()] = "",
    ) -> fastapi_responses.HTMLResponse:
        handlers = {"confirm": applications.confirm, "dismiss": applications.dismiss}
        if action not in handlers:
            raise fastapi.HTTPException(status_code=404, detail="Unknown action")
        if handlers[action](session, app_id) is None:
            raise fastapi.HTTPException(status_code=404, detail="Application not found")
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session, company)
        )

    return app


def _save_debug(path: pathlib.Path, text: str) -> None:
    # Local diagnostics only: data/ is git-ignored and may hold page content.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def _applications_context(session: sa_orm.Session, company: str = "") -> dict:
    rows = session.execute(
        sa.select(models.Application, models.EmailMessage.gmail_thread_id)
        .outerjoin(
            models.EmailMessage, models.Application.email_message_id == models.EmailMessage.id
        )
        .options(sa_orm.joinedload(models.Application.company))
        .where(models.Application.review != models.ApplicationReview.DISMISSED)
        .order_by(models.Application.applied_at.desc())
    ).all()
    needle = company.strip().casefold()
    items = [
        {
            "a": a,
            "status": applications.application_status(session, a),
            "gmail": _gmail_link(thread),
        }
        for a, thread in rows
        if needle in a.company.name.casefold()
    ]
    suggested = [i for i in items if i["a"].review is models.ApplicationReview.SUGGESTED]
    confirmed = [i for i in items if i["a"].review is models.ApplicationReview.CONFIRMED]
    return {
        "suggested": suggested,
        "confirmed": confirmed,
        "has_any": bool(items),
        "has_rows": bool(rows),
        "company": company,
    }


def _load_opportunities(
    session: sa_orm.Session, states: list[models.OpportunityState] | None = None
):
    query = sa.select(models.Opportunity).options(
        sa_orm.joinedload(models.Opportunity.company),
        sa_orm.selectinload(models.Opportunity.source_records).selectinload(
            models.SourceRecord.snapshots
        ),
    )
    if states is not None:
        query = query.where(models.Opportunity.state.in_(states))
    return session.scalars(query).all()


def _get_opportunity(session: sa_orm.Session, opportunity_id: int) -> models.Opportunity:
    opportunity = session.get(models.Opportunity, opportunity_id)
    if opportunity is None:
        raise fastapi.HTTPException(status_code=404, detail="Opportunity not found")
    return opportunity


def _panel_response(
    request: fastapi.Request,
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    tab: str,
    filters: Filters,
) -> fastapi_responses.HTMLResponse:
    """The panel redrawn for this vacancy, and the list items of its company out of band
    (updated, or deleted when they left the tab), and the tab counts (#158)."""
    session.refresh(opportunity)
    states = TABS[tab][1]
    rejected = rejections.latest_rejections(session)

    def visible(o: models.Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = _row_context(session, opportunity, rejected) | _claude_context(
        session, opportunity.id
    )
    context |= _tabs_context(session, tab, filters) | {"mode": "panel"}
    company = session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.company_id == opportunity.company_id)
    ).all()
    context["items_shown"] = [_row_context(session, o, rejected) for o in company if visible(o)]
    context["items_gone"] = [o.id for o in company if not visible(o)]
    return templates.TemplateResponse(request, "_panel_and_items.html", context)


def _company_rows(
    request: fastapi.Request,
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    tab: str,
    filters: Filters,
) -> fastapi_responses.HTMLResponse:
    """This row (gone when it leaves the tab), the company's other rows out of band, and the
    tab counts: history and penalties are per company, the queue is not re-rendered (#138)."""
    session.refresh(opportunity)
    states = TABS[tab][1]
    rejected = rejections.latest_rejections(session)

    def visible(o: models.Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = _row_context(session, opportunity, rejected) | _tabs_context(session, tab, filters)
    context["show_row"] = visible(opportunity)
    others = session.scalars(
        sa.select(models.Opportunity).where(
            models.Opportunity.company_id == opportunity.company_id,
            models.Opportunity.id != opportunity.id,
        )
    ).all()
    context["company_rows"] = [_row_context(session, o, rejected) for o in others if visible(o)]
    return templates.TemplateResponse(request, "_row_and_tabs.html", context)


def _claude_context(session: sa_orm.Session, opportunity_id: int) -> dict:
    current = claude_analysis.latest(session, opportunity_id)
    chat_pending = claude_analysis.chat_pending(session, opportunity_id)
    return {
        "opportunity_id": opportunity_id,
        "claude": current,
        "messages": claude_analysis.messages(session, opportunity_id),
        "chat_ready": claude_analysis.latest_done(session, opportunity_id) is not None,
        "quick_prompts": claude_prompt.quick_prompts(_profile(session)),
        "polling": chat_pending
        or (current is not None and current.state in claude_analysis.PENDING),
    }


CAPTURE_KEY = "capture"
# seconds the capture tab stays open after a good result (#221)
CLOSE_AFTER = 1.5


def load_close_after(session: sa_orm.Session) -> float:
    row = session.get(models.AppSetting, CAPTURE_KEY)
    value = row.value.get("close_after") if row and isinstance(row.value, dict) else None
    return float(value) if isinstance(value, int | float) else CLOSE_AFTER


def save_close_after(session: sa_orm.Session, seconds: float) -> None:
    row = session.get(models.AppSetting, CAPTURE_KEY) or models.AppSetting(key=CAPTURE_KEY)
    row.value = {"close_after": seconds}
    session.add(row)
    session.commit()


def _settings_context(session: sa_orm.Session, settings: config.Settings) -> dict:
    return {
        "rules": auto_ignore.load_rules(session),
        "claude_kinds": auto_ignore.CLAUDE_KINDS,
        "claude": claude_analysis.load_claude_settings(session),
        "pause": linkedin_public.load_pause(session),
        "chrome_pause": linkedin_chrome.load_chrome_pause(session),
        "chrome_on": settings.linkedin_chrome,
        "close_after": load_close_after(session),
        "models": claude_analysis.MODELS,
        "languages": claude_analysis.LANGUAGES,
        "cv_path": settings.cv_path,
        "message": None,
    }


@dataclasses.dataclass
class LogDay:
    """One day of the LinkedIn request log: requests, saves, errors and blocks."""

    day: str
    requests: int = 0
    saved: int = 0
    errors: int = 0
    stops: int = 0
    blocked_at: dt.datetime | None = None
    blocked_reason: str | None = None


def _fetch_log(session: sa_orm.Session, days: int) -> tuple[list[dict], list[LogDay]]:
    """LinkedIn guest requests of the last ``days`` days, newest first, and one line per
    local day (#205)."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(1, min(days, 90)))
    fetches = session.scalars(
        sa.select(models.LinkedInFetch)
        .where(models.LinkedInFetch.at >= since.replace(tzinfo=None))
        .order_by(models.LinkedInFetch.at.desc(), models.LinkedInFetch.id.desc())
    ).all()
    # Requests logged before #205 have only the LinkedIn job id.
    old = {f.job_id for f in fetches if not f.opportunity_id and f.job_id}
    by_job = dict(
        session.execute(
            sa.select(models.SourceRecord.external_id, models.SourceRecord.opportunity_id).where(
                models.SourceRecord.source_type.in_(linkedin_alerts.LINKEDIN_SOURCES),
                models.SourceRecord.external_id.in_(old),
            )
        ).all()
    )
    vacancy_of = {f.id: f.opportunity_id or by_job.get(f.job_id) for f in fetches}
    ids = {i for i in vacancy_of.values() if i}
    query = sa.select(models.Opportunity).where(models.Opportunity.id.in_(ids))
    titles = {
        o.id: o
        for o in session.scalars(query.options(sa_orm.joinedload(models.Opportunity.company)))
    }
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


def _first_gap(analysis, profile: profile_stack.Profile | None) -> str:
    """The first required technology not known, to prefill the Stack chip."""
    for item in (analysis.stack or []) if analysis else []:
        if item.get("status") == "required" and (
            not profile or profile.level(item["skill"]) != "known"
        ):
            return item["skill"]
    return ""


def _profile(session: sa_orm.Session) -> profile_stack.Profile | None:
    settings = session.info.get("settings")
    return profile_stack.current_profile(settings.profile) if settings else None


def _score(
    session: sa_orm.Session, opportunity: models.Opportunity, rejected_at: dt.datetime | None
) -> tuple[float | None, list, str | None]:
    """The score shown and sorted by, its extra reasons, and the description used."""
    eff_score = rejections.effective_score(opportunity.score, rejected=rejected_at is not None)
    extra_reasons = []
    description = scoring_description.latest_description(opportunity)
    effects = (
        notes.location_penalty(session, opportunity),
        scoring_language.language_risk(opportunity.title, description),
        scoring_description.python_risk(opportunity.title, description),
        *scoring_findings.risks(scoring_findings.latest_analysis(opportunity), _profile(session)),
    )
    for penalty in effects:
        if penalty is None:
            continue
        points, reason = penalty
        extra_reasons.append(reason)
        if eff_score is not None:
            eff_score = round(max(0.0, eff_score + points), 1)
    return eff_score, extra_reasons, description


def _row_context(
    session: sa_orm.Session, opportunity: models.Opportunity, rejected: dict | None = None
) -> dict:
    if rejected is None:
        rejected = rejections.latest_rejections(session)
    rejected_at = rejected.get(opportunity.company_id)
    history = contacts.company_history(session, opportunity.company_id)
    eff_score, extra_reasons, description = _score(session, opportunity, rejected_at)
    python_check = scoring_description.check_python(description) if description else None
    others = contacts.other_vacancies(session, opportunity.company_id, opportunity.id)
    applied = [e.at for e in history if e.kind == "applied"]
    applied += [at for o, at in others if o.state is models.OpportunityState.APPLIED]
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
        "claude": claude_analysis.latest(session, opportunity.id),
        "chat_count": len(claude_analysis.messages(session, opportunity.id)),
        "suggestions": suggestions.suggest(
            opportunity,
            description=description,
            analysis=scoring_findings.latest_analysis(opportunity),
            profile=_profile(session),
            claude=claude_analysis.latest(session, opportunity.id),
        ),
        "stack_hint": _first_gap(scoring_findings.latest_analysis(opportunity), _profile(session)),
        "country": notes.country_from_location(opportunity.location) or "",
        "reason_kinds": [k for k in models.NoteKind if k is not models.NoteKind.NOTE],
    }


def _tabs_context(session: sa_orm.Session, tab: str, filters: Filters | None = None) -> dict:
    filters = filters or Filters()
    counts: dict[models.OpportunityState, int] = {}
    rows = session.execute(
        sa.select(
            models.Opportunity.id,
            models.Opportunity.state,
            models.Opportunity.location,
            models.Company.name,
            models.Opportunity.title,
        ).join(models.Opportunity.company)
    ).all()
    described = set(
        session.scalars(
            sa.select(models.SourceRecord.opportunity_id).join(models.JobSnapshot).distinct()
        )
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
    session: sa_orm.Session, tab: str = DEFAULT_TAB, filters: Filters | None = None, page: int = 1
) -> dict:
    filters = filters or Filters()
    rejected = rejections.latest_rejections(session)
    opportunities = [
        o for o in _load_opportunities(session, TABS[tab][1]) if filters.matches_opportunity(o)
    ]
    # Sort by the score alone; the full row (history, Claude, hints) only for the page (#180).
    keyed = [
        {"o": o, "rejected_at": rejected.get(o.company_id)}
        | {"eff_score": _score(session, o, rejected.get(o.company_id))[0]}
        for o in opportunities
    ]
    # your last action first (#199)
    if filters.sort == "recent":
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
    elif filters.sort == "claude":
        # Claude's match % first, unanalysed at the bottom (#207)
        matches = claude_analysis.latest_matches(session)
        # equal % keep the score order
        keyed.sort(key=_queue_order)
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
        "claude_status": claude_analysis.status(session),
        "added": None,
    }


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _visible_history_count(history: list) -> int:
    """How many contacts the "contacted" badge counts: the Chrome fill's marks are not (#272)."""
    return sum(e.kind not in models.FILL_EVENT_KINDS for e in history)


def last_actions(session: sa_orm.Session) -> dict[int, tuple[dt.datetime, str]]:
    """Per vacancy: when you last did something with it, and what (#199)."""
    found: dict[int, tuple[dt.datetime, str]] = {}

    def keep(oid: int | None, at: dt.datetime | None, what: str) -> None:
        if oid is None or at is None:
            return
        at = _utc(at)
        if oid not in found or at > found[oid][0]:
            found[oid] = (at, what)

    for oid, at, state in session.execute(
        sa.select(
            models.Opportunity.id, models.Opportunity.state_changed_at, models.Opportunity.state
        )
    ):
        keep(oid, at, state.value.replace("_", " "))
    for oid, at in session.execute(
        sa.select(models.Application.opportunity_id, models.Application.applied_at)
    ):
        keep(oid, at, "applied")
    for oid, at in session.execute(
        sa.select(models.Rejection.opportunity_id, models.Rejection.rejected_at)
    ):
        keep(oid, at, "rejected")
    for oid, at, kind in session.execute(
        sa.select(
            models.OpportunityNote.opportunity_id,
            models.OpportunityNote.created_at,
            models.OpportunityNote.kind,
        )
    ):
        keep(oid, at, "note" if kind is models.NoteKind.NOTE else "ignored")
    for oid, at, kind in session.execute(
        sa.select(
            models.CompanyEvent.opportunity_id, models.CompanyEvent.at, models.CompanyEvent.kind
        ).where(models.CompanyEvent.kind.not_in(models.FILL_EVENT_KINDS))
    ):
        keep(oid, at, kind.replace("_", " "))
    for oid, at in session.execute(
        sa.select(models.ClaudeMessage.opportunity_id, models.ClaudeMessage.created_at).where(
            models.ClaudeMessage.role == "user"
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


def _manual_states() -> list[models.OpportunityState]:
    automatic = (models.OpportunityState.NEW, models.OpportunityState.AUTO_IGNORE)
    return [s for s in models.OpportunityState if s not in automatic]
