from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import Settings, get_settings
from ..db import make_engine, make_session_factory
from ..models import Opportunity, OpportunityState

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
        opportunities = session.scalars(
            select(Opportunity).options(joinedload(Opportunity.company))
        ).all()
        opportunities = sorted(
            opportunities, key=lambda o: (_STATE_ORDER.index(o.state), -o.created_at.timestamp())
        )
        return templates.TemplateResponse(
            request,
            "queue.html",
            {"opportunities": opportunities, "states": _manual_states()},
        )

    @app.post("/opportunities/{opportunity_id}/state", response_class=HTMLResponse)
    def set_state(
        request: Request,
        opportunity_id: int,
        state: Annotated[OpportunityState, Form()],
        session: SessionDep,
    ) -> HTMLResponse:
        opportunity = session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        opportunity.state = state
        session.commit()
        return templates.TemplateResponse(
            request, "_row.html", {"o": opportunity, "states": _manual_states()}
        )

    return app


def _manual_states() -> list[OpportunityState]:
    return [s for s in OpportunityState if s is not OpportunityState.NEW]
