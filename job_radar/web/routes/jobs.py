"""Routes of the background jobs: Sync, Descriptions, Chrome fill and the request log.

Usage:
    import job_radar.web.routes.jobs as routes_jobs
"""

import dataclasses
import datetime as dt

import fastapi
import fastapi.responses as fastapi_responses
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.jobs.pipeline as jobs_pipeline
import job_radar.models as models
import job_radar.sources.linkedin.alerts as linkedin_alerts
import job_radar.web.context as web_context
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()


QUEUE_REDRAW = "jr-refresh-queue"


def _redraw_when_over(
    response: fastapi_responses.HTMLResponse, polled: bool, state: str, saved: bool
) -> fastapi_responses.HTMLResponse:
    """The poll that sees a job end, while the page watched it run, asks the queue to redraw
    its list and counters (#263). A page loaded later already shows the new state."""
    if polled and saved and state in ("done", "failed"):
        response.headers["HX-Trigger"] = QUEUE_REDRAW
    return response


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
        at = web_context.utc(f.at)
        rows.append({"f": f, "at": at, "o": titles.get(vacancy_of[f.id])})
        day = by_day.setdefault(
            web_templating.local_time(at, "%d.%m"), LogDay(web_templating.local_time(at, "%d.%m"))
        )
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


@router.post("/sync", response_class=fastapi_responses.HTMLResponse)
def start_sync(
    sync_job: web_deps.SyncJobDep, sync_runner: web_deps.SyncRunnerDep, request: fastapi.Request
) -> fastapi_responses.HTMLResponse:
    # A request while a sync runs (double click, another tab) starts nothing and shows the
    # running one with "already running" (#72).
    started = sync_job.start(sync_runner)
    return web_templating.templates.TemplateResponse(
        request, "_sync_status.html", {"job": sync_job, "already": not started}
    )


@router.get("/sync/status", response_class=fastapi_responses.HTMLResponse)
def sync_status(
    sync_job: web_deps.SyncJobDep, request: fastapi.Request, polled: bool = False
) -> fastapi_responses.HTMLResponse:
    response = web_templating.templates.TemplateResponse(
        request, "_sync_status.html", {"job": sync_job}
    )
    return _redraw_when_over(response, polled, sync_job.state, saved=True)


def descriptions_status(
    request: fastapi.Request, off: bool = False
) -> fastapi_responses.HTMLResponse:
    context = {"job": request.app.state.description_job, "off": off}
    return web_templating.templates.TemplateResponse(request, "_descriptions_status.html", context)


@router.post("/descriptions", response_class=fastapi_responses.HTMLResponse)
def start_description_job(
    settings: web_deps.SettingsDep,
    start_descriptions: web_deps.StartDescriptionsDep,
    request: fastapi.Request,
) -> fastapi_responses.HTMLResponse:
    if not jobs_pipeline.descriptions_on(settings):
        return descriptions_status(request, off=True)
    # a second click just shows the running one
    start_descriptions()
    return descriptions_status(request)


@router.post("/descriptions/stop", response_class=fastapi_responses.HTMLResponse)
def stop_description_job(
    description_job: web_deps.DescriptionJobDep, request: fastapi.Request
) -> fastapi_responses.HTMLResponse:
    description_job.stop()
    return descriptions_status(request)


@router.get("/descriptions/status", response_class=fastapi_responses.HTMLResponse)
def description_job_status(
    description_job: web_deps.DescriptionJobDep, request: fastapi.Request, polled: bool = False
) -> fastapi_responses.HTMLResponse:
    job = description_job
    return _redraw_when_over(descriptions_status(request), polled, job.state, job.saved > 0)


def chrome_status(
    request: fastapi.Request, off: bool = False, busy: bool = False
) -> fastapi_responses.HTMLResponse:
    context = {"job": request.app.state.chrome_job, "off": off, "busy": busy}
    return web_templating.templates.TemplateResponse(request, "_chrome_status.html", context)


@router.post("/chrome-fill", response_class=fastapi_responses.HTMLResponse)
def start_chrome_fill(
    settings: web_deps.SettingsDep,
    description_job: web_deps.DescriptionJobDep,
    chrome_job: web_deps.ChromeJobDep,
    chrome_runner: web_deps.ChromeRunnerDep,
    request: fastapi.Request,
) -> fastapi_responses.HTMLResponse:
    if not settings.linkedin_chrome:
        return chrome_status(request, off=True)
    if description_job.state == "running":
        return chrome_status(request, busy=True)
    # a second click just shows the running one
    chrome_job.start(chrome_runner)
    return chrome_status(request)


@router.post("/chrome-fill/stop", response_class=fastapi_responses.HTMLResponse)
def stop_chrome_fill(
    chrome_job: web_deps.ChromeJobDep, request: fastapi.Request
) -> fastapi_responses.HTMLResponse:
    chrome_job.stop()
    return chrome_status(request)


@router.get("/chrome-fill/status", response_class=fastapi_responses.HTMLResponse)
def chrome_fill_status(
    chrome_job: web_deps.ChromeJobDep, request: fastapi.Request, polled: bool = False
) -> fastapi_responses.HTMLResponse:
    job = chrome_job
    return _redraw_when_over(chrome_status(request), polled, job.state, job.saved > 0)


@router.get("/linkedin/log", response_class=fastapi_responses.HTMLResponse)
def linkedin_log(
    request: fastapi.Request, session: web_deps.SessionDep, days: int = 7
) -> fastapi_responses.HTMLResponse:
    rows, summary = _fetch_log(session, days)
    context = {"rows": rows, "summary": summary, "days": days}
    return web_templating.templates.TemplateResponse(request, "linkedin_log.html", context)
