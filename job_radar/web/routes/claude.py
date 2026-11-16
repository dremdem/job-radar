"""Routes of the Claude analysis, the chat and the memory page.

Usage:
    import job_radar.web.routes.claude as routes_claude
"""

import typing as t

import fastapi
import fastapi.responses as fastapi_responses
import sqlalchemy.orm as sa_orm

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.memory as claude_memory
import job_radar.web.context as web_context
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()


@router.get("/opportunities/{opportunity_id}/claude", response_class=fastapi_responses.HTMLResponse)
def opportunity_claude(
    request: fastapi.Request, opportunity_id: int, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    web_context.get_opportunity(session, opportunity_id)
    context = web_context.claude_context(session, opportunity_id)
    return web_templating.templates.TemplateResponse(request, "_claude_section.html", context)


@router.post(
    "/opportunities/{opportunity_id}/claude", response_class=fastapi_responses.HTMLResponse
)
def opportunity_analyze(
    request: fastapi.Request, opportunity_id: int, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    web_context.get_opportunity(session, opportunity_id)
    claude_analysis.enqueue(session, [opportunity_id])
    request.app.state.claude_worker.wake()
    context = web_context.claude_context(session, opportunity_id)
    return web_templating.templates.TemplateResponse(request, "_claude_section.html", context)


@router.post("/opportunities/{opportunity_id}/chat", response_class=fastapi_responses.HTMLResponse)
def opportunity_ask(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    text: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    web_context.get_opportunity(session, opportunity_id)
    if claude_analysis.ask(session, opportunity_id, text) is None:
        raise fastapi.HTTPException(status_code=422, detail="Empty question or no analysis yet")
    request.app.state.claude_worker.wake()
    context = web_context.claude_context(session, opportunity_id)
    return web_templating.templates.TemplateResponse(request, "_claude_section.html", context)


@router.post("/claude/analyze", response_class=fastapi_responses.HTMLResponse)
def claude_analyze(
    request: fastapi.Request,
    session: web_deps.SessionDep,
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
    return web_templating.templates.TemplateResponse(request, "_claude_status.html", context)


@router.get("/claude/status", response_class=fastapi_responses.HTMLResponse)
def claude_status(
    request: fastapi.Request, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    context = {
        "claude_status": claude_analysis.status(session),
        "added": None,
        "worker_off": not request.app.state.claude_worker_on,
    }
    return web_templating.templates.TemplateResponse(request, "_claude_status.html", context)


@router.post("/settings/claude", response_class=fastapi_responses.HTMLResponse)
def save_claude_settings(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
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
    context = web_context.settings_context(session, settings) | {
        "message": "Claude settings saved."
    }
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.get("/memory", response_class=fastapi_responses.HTMLResponse)
def memory_page(
    request: fastapi.Request, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    return _memory_response(request, session)


@router.post("/memory", response_class=fastapi_responses.HTMLResponse)
def save_memory(
    request: fastapi.Request,
    session: web_deps.SessionDep,
    kind: t.Annotated[str, fastapi.Form()],
    name: t.Annotated[str, fastapi.Form(max_length=64)],
    description: t.Annotated[str, fastapi.Form(max_length=255)] = "",
    body: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    saved = claude_memory.save(session, kind, name, description, body, by="you")
    if saved is None:
        raise fastapi.HTTPException(status_code=422, detail="Unknown kind or empty name")
    return _memory_response(request, session, f"Saved {saved}.")


@router.post("/memory/{name}/delete", response_class=fastapi_responses.HTMLResponse)
def delete_memory(
    request: fastapi.Request, session: web_deps.SessionDep, name: str
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
    return web_templating.templates.TemplateResponse(request, "memory.html", context)
