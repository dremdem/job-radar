from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import applications as apps
from ..config import Settings, get_settings
from ..contacts import company_history
from ..db import make_engine, make_session_factory
from ..models import (
    Application,
    ApplicationReview,
    EmailMessage,
    Opportunity,
    OpportunityState,
    SourceRecord,
)
from ..rejections import effective_score, latest_rejections, toggle_manual_rejection

HISTORY_SHOWN = 8
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Queue order: undecided first, then the ones worth a look.
_STATE_ORDER = [
    OpportunityState.NEW,
    OpportunityState.INTERESTED,
    OpportunityState.APPLIED,
    OpportunityState.IGNORE,
]


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
    def queue(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(request, "queue.html", _queue_context(session))

    @app.post("/opportunities/{opportunity_id}/state", response_class=HTMLResponse)
    def set_state(
        request: Request,
        opportunity_id: int,
        state: Annotated[OpportunityState, Form()],
        session: SessionDep,
    ) -> HTMLResponse:
        opportunity = _get_opportunity(session, opportunity_id)
        opportunity.state = state
        session.commit()
        return templates.TemplateResponse(request, "_row.html", _row_context(session, opportunity))

    @app.post("/opportunities/{opportunity_id}/rejected", response_class=HTMLResponse)
    def set_rejected(request: Request, opportunity_id: int, session: SessionDep) -> HTMLResponse:
        # Rejection is per company, so a toggle can move several rows: re-render the queue.
        opportunity = _get_opportunity(session, opportunity_id)
        toggle_manual_rejection(session, opportunity)
        session.commit()
        return templates.TemplateResponse(request, "_queue.html", _queue_context(session))

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


def _load_opportunities(session: Session):
    return session.scalars(
        select(Opportunity).options(
            joinedload(Opportunity.company),
            selectinload(Opportunity.source_records).selectinload(SourceRecord.snapshots),
        )
    ).all()


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
    applied = [e.at for e in history if e.kind == "applied"]
    return {
        "history": history[:HISTORY_SHOWN],
        "history_count": len(history),
        "applied_before": max(applied) if applied else None,
        "o": opportunity,
        "states": _manual_states(),
        "rejected_at": rejected_at,
        "eff_score": effective_score(opportunity.score, rejected=rejected_at is not None),
    }


def _queue_context(session: Session) -> dict:
    rejected = latest_rejections(session)
    rows = [_row_context(session, o, rejected) for o in _load_opportunities(session)]
    rows.sort(key=_queue_order)
    # _queue.html includes _row.html per row; the buttons need the states in scope.
    return {"rows": rows, "has_any": bool(rows), "states": _manual_states()}


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
