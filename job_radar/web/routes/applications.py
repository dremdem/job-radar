"""Routes of the Applications page.

Usage:
    import job_radar.web.routes.applications as routes_applications
"""

import typing as t

import fastapi
import fastapi.responses as fastapi_responses
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.history.applications as history_applications
import job_radar.models as models
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()


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
            "status": history_applications.application_status(session, a),
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


@router.get("/applications", response_class=fastapi_responses.HTMLResponse)
def applications_page(
    request: fastapi.Request, session: web_deps.SessionDep, company: str = ""
) -> fastapi_responses.HTMLResponse:
    return web_templating.templates.TemplateResponse(
        request, "applications.html", _applications_context(session, company)
    )


@router.post("/applications/confirm-all", response_class=fastapi_responses.HTMLResponse)
def applications_confirm_all(
    request: fastapi.Request,
    session: web_deps.SessionDep,
    company: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    # with a filter: only the suggestions shown
    history_applications.confirm_all(session, company)
    return web_templating.templates.TemplateResponse(
        request, "_applications.html", _applications_context(session, company)
    )


@router.post("/applications/{app_id}/{action}", response_class=fastapi_responses.HTMLResponse)
def applications_review(
    request: fastapi.Request,
    app_id: int,
    action: str,
    session: web_deps.SessionDep,
    company: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    handlers = {
        "confirm": history_applications.confirm,
        "dismiss": history_applications.dismiss,
    }
    if action not in handlers:
        raise fastapi.HTTPException(status_code=404, detail="Unknown action")
    if handlers[action](session, app_id) is None:
        raise fastapi.HTTPException(status_code=404, detail="Application not found")
    return web_templating.templates.TemplateResponse(
        request, "_applications.html", _applications_context(session, company)
    )
