import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import applications as apps
from ..config import Settings, get_settings
from ..contacts import company_history
from ..db import make_engine, make_session_factory
from ..language import language_risk
from ..linkedin.capture import import_capture
from ..models import (
    Application,
    ApplicationReview,
    Company,
    EmailMessage,
    NoteKind,
    Opportunity,
    OpportunityState,
    SourceRecord,
)
from ..notes import (
    add_note,
    country_from_location,
    delete_note,
    ignore_with_reason,
    location_penalty,
)
from ..rejections import effective_score, latest_rejections, toggle_manual_rejection

HISTORY_SHOWN = 8
BOOKMARKLETS = {
    name: (Path(__file__).parent / f"{name}.js").read_text()
    for name in ("bookmarklet", "bookmarklet_debug")
}


def bookmarklet(base_url: str, name: str = "bookmarklet") -> str:
    """The bookmarklet as a javascript: URL; comments and indentation stripped."""
    code = re.sub(r"/\*.*?\*/", "", BOOKMARKLETS[name], flags=re.S)
    code = " ".join(line.strip() for line in code.splitlines() if line.strip())
    return "javascript:" + code.replace("__BASE__", base_url.rstrip("/"))


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Queue order: undecided first, then the ones worth a look.
_STATE_ORDER = [
    OpportunityState.NEW,
    OpportunityState.INTERESTED,
    OpportunityState.APPLIED,
    OpportunityState.IGNORE,
]


# Queue tabs: key -> (label, states shown; None = all).
TABS: dict[str, tuple[str, list[OpportunityState] | None]] = {
    "review": ("To review", [OpportunityState.NEW]),
    "interested": ("Interested", [OpportunityState.INTERESTED]),
    "applied": ("Applied", [OpportunityState.APPLIED]),
    "ignored": ("Ignored", [OpportunityState.IGNORE]),
    "all": ("All", None),
}
DEFAULT_TAB = "review"


@dataclass(frozen=True)
class Filters:
    """Queue filters: any part of the text, case-insensitive (non-ASCII too)."""

    country: str = ""
    company: str = ""

    def matches(self, location: str | None, company: str) -> bool:
        return self.country.strip().casefold() in (location or "").casefold() and (
            self.company.strip().casefold() in company.casefold()
        )

    def href(self, tab: str) -> str:
        params = {"tab": tab} | {
            k: v for k, v in (("country", self.country), ("company", self.company)) if v
        }
        return "/?" + urlencode(params)


def _tab(value: str | None) -> str:
    return value if value in TABS else DEFAULT_TAB


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.session_factory = make_session_factory(engine)
        yield
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
    ) -> HTMLResponse:
        filters = Filters(country, company)
        return templates.TemplateResponse(
            request, "queue.html", _queue_context(session, _tab(tab), filters)
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
    ) -> HTMLResponse:
        filters = Filters(country, company)
        tab = _tab(tab)
        opportunity = _get_opportunity(session, opportunity_id)
        opportunity.state = state
        session.commit()
        if tab == "all":
            # Keep the row in place; refresh the tab counts out of band.
            context = _row_context(session, opportunity) | _tabs_context(session, tab, filters)
            return templates.TemplateResponse(request, "_row_and_tabs.html", context)
        return templates.TemplateResponse(
            request, "_queue.html", _queue_context(session, tab, filters)
        )

    @app.post("/opportunities/{opportunity_id}/rejected", response_class=HTMLResponse)
    def set_rejected(
        request: Request,
        opportunity_id: int,
        session: SessionDep,
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company)
        # Rejection is per company, so a toggle can move several rows: re-render the queue.
        opportunity = _get_opportunity(session, opportunity_id)
        toggle_manual_rejection(session, opportunity)
        session.commit()
        return templates.TemplateResponse(
            request, "_queue.html", _queue_context(session, _tab(tab), filters)
        )

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
    ) -> HTMLResponse:
        filters = Filters(country, company)
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
        return templates.TemplateResponse(
            request, "_queue.html", _queue_context(session, _tab(tab), filters)
        )

    @app.post("/notes/{note_id}/delete", response_class=HTMLResponse)
    def remove_note(
        request: Request,
        note_id: int,
        session: SessionDep,
        tab: Annotated[str, Form()] = DEFAULT_TAB,
        country: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        filters = Filters(country, company)
        if not delete_note(session, note_id):
            raise HTTPException(status_code=404, detail="Note not found")
        return templates.TemplateResponse(
            request, "_queue.html", _queue_context(session, _tab(tab), filters)
        )

    @app.get("/capture", response_class=HTMLResponse)
    def capture_page(request: Request) -> HTMLResponse:
        base = str(request.base_url)
        context = {
            "bookmarklet": bookmarklet(base),
            "debug_bookmarklet": bookmarklet(base, "bookmarklet_debug"),
        }
        return templates.TemplateResponse(request, "capture.html", context)

    @app.get("/capture/debug", response_class=HTMLResponse)
    def capture_debug_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "capture_debug.html", {})

    @app.post("/api/capture")
    def capture(session: SessionDep, payload: Annotated[dict, Body()]) -> dict[str, int]:
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
    def applications_page(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "applications.html", _applications_context(session)
        )

    @app.post("/applications/confirm-all", response_class=HTMLResponse)
    def applications_confirm_all(request: Request, session: SessionDep) -> HTMLResponse:
        apps.confirm_all(session)
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session)
        )

    @app.post("/applications/{app_id}/{action}", response_class=HTMLResponse)
    def applications_review(
        request: Request, app_id: int, action: str, session: SessionDep
    ) -> HTMLResponse:
        handlers = {"confirm": apps.confirm, "dismiss": apps.dismiss}
        if action not in handlers:
            raise HTTPException(status_code=404, detail="Unknown action")
        if handlers[action](session, app_id) is None:
            raise HTTPException(status_code=404, detail="Application not found")
        return templates.TemplateResponse(
            request, "_applications.html", _applications_context(session)
        )

    return app


def _save_debug(path: Path, text: str) -> None:
    # Local diagnostics only: data/ is git-ignored and may hold page content.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def _applications_context(session: Session) -> dict:
    rows = session.execute(
        select(Application, EmailMessage.gmail_thread_id)
        .outerjoin(EmailMessage, Application.email_message_id == EmailMessage.id)
        .options(joinedload(Application.company))
        .where(Application.review != ApplicationReview.DISMISSED)
        .order_by(Application.applied_at.desc())
    ).all()
    items = [
        {"a": a, "status": apps.application_status(session, a), "gmail": _gmail_link(thread)}
        for a, thread in rows
    ]
    suggested = [i for i in items if i["a"].review is ApplicationReview.SUGGESTED]
    confirmed = [i for i in items if i["a"].review is ApplicationReview.CONFIRMED]
    return {"suggested": suggested, "confirmed": confirmed, "has_any": bool(items)}


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


def _row_context(session: Session, opportunity: Opportunity, rejected: dict | None = None) -> dict:
    if rejected is None:
        rejected = latest_rejections(session)
    rejected_at = rejected.get(opportunity.company_id)
    history = company_history(session, opportunity.company_id)
    eff_score = effective_score(opportunity.score, rejected=rejected_at is not None)
    extra_reasons = []
    for penalty in (location_penalty(session, opportunity), language_risk(opportunity.title)):
        if penalty is None:
            continue
        points, reason = penalty
        extra_reasons.append(reason)
        if eff_score is not None:
            eff_score = round(max(0.0, eff_score + points), 1)
    applied = [e.at for e in history if e.kind == "applied"]
    return {
        "history": history[:HISTORY_SHOWN],
        "history_count": len(history),
        "applied_before": max(applied) if applied else None,
        "o": opportunity,
        "states": _manual_states(),
        "rejected_at": rejected_at,
        "eff_score": eff_score,
        "extra_reasons": extra_reasons,
        "country": country_from_location(opportunity.location) or "",
        "reason_kinds": [k for k in NoteKind if k is not NoteKind.NOTE],
    }


def _tabs_context(session: Session, tab: str, filters: Filters | None = None) -> dict:
    filters = filters or Filters()
    counts: dict[OpportunityState, int] = {}
    rows = session.execute(
        select(Opportunity.state, Opportunity.location, Company.name).join(Opportunity.company)
    ).all()
    for state, location, company in rows:
        if filters.matches(location, company):
            counts[state] = counts.get(state, 0) + 1
    tabs = [
        (
            key,
            label,
            sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states),
            filters.href(key),
        )
        for key, (label, states) in TABS.items()
    ]
    return {"tab": tab, "tabs": tabs, "total": len(rows), "filters": filters}


def _queue_context(
    session: Session, tab: str = DEFAULT_TAB, filters: Filters | None = None
) -> dict:
    filters = filters or Filters()
    rejected = latest_rejections(session)
    opportunities = [
        o
        for o in _load_opportunities(session, TABS[tab][1])
        if filters.matches(o.location, o.company.name)
    ]
    rows = [_row_context(session, o, rejected) for o in opportunities]
    rows.sort(key=_queue_order)
    context = _tabs_context(session, tab, filters)
    # _queue.html includes _row.html per row; the buttons need the states in scope.
    return context | {"rows": rows, "has_any": context["total"] > 0, "states": _manual_states()}


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
    return [s for s in OpportunityState if s is not OpportunityState.NEW]
