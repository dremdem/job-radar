"""Routes of the Settings, Profile and Skills pages.

Usage:
    import job_radar.web.routes.settings as routes_settings
"""

import dataclasses
import typing as t

import fastapi
import fastapi.responses as fastapi_responses

import job_radar.ai.local.analysis as local_analysis
import job_radar.ai.skills as ai_skills
import job_radar.profile.stack as profile_stack
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.sources.linkedin.chrome as linkedin_chrome
import job_radar.sources.linkedin.public as linkedin_public
import job_radar.web.context as web_context
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()


@router.get("/settings", response_class=fastapi_responses.HTMLResponse)
def settings_page(
    settings: web_deps.SettingsDep, request: fastapi.Request, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    context = web_context.settings_context(session, settings)
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings", response_class=fastapi_responses.HTMLResponse)
def save_settings(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
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
    stack_title: t.Annotated[str | None, fastapi.Form()] = None,
    stacks: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    rules = review_auto_ignore.Rules(
        hybrid=hybrid is not None,
        applied=applied is not None,
        ignored=ignored is not None,
        rejected=rejected is not None,
        no_python=no_python is not None,
        ai_location=ai_location is not None,
        language=language is not None,
        unavailable=unavailable is not None,
        days=days,
        countries=review_auto_ignore.parse_countries(countries),
        claude_any=claude_any is not None,
        claude_kinds=review_auto_ignore.parse_claude_kinds(claude_kinds),
        stack_title=stack_title is not None,
        stacks=review_auto_ignore.parse_stacks(stacks),
    )
    review_auto_ignore.save_rules(session, rules)
    context = web_context.settings_context(session, settings)
    context["message"] = "Saved. New vacancies follow these rules."
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.get("/skills", response_class=fastapi_responses.HTMLResponse)
def skills_page(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
    days: int = 0,
    scope: str = "open",
    sort: str = "unlock",
    dir: str = "desc",
) -> fastapi_responses.HTMLResponse:
    everything = scope == "all"
    report = ai_skills.build_report(
        session,
        profile_stack.load_profile(settings.profile),
        days=days or None,
        everything=everything,
    )
    report.rows = ai_skills.sort_rows(report.rows, sort, dir)
    context = {"report": report, "days": days, "scope": scope, "message": ""}
    context |= {"sort": sort if sort in ai_skills.SORTS else "unlock", "dir": dir}
    return web_templating.templates.TemplateResponse(request, "skills.html", context)


@router.post("/skills/level", response_class=fastapi_responses.HTMLResponse)
def skills_level(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
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
    report = ai_skills.build_report(
        session,
        profile_stack.load_profile(settings.profile),
        days=days or None,
        everything=scope == "all",
    )
    message = f"{name.strip()} is now {level.rstrip('s')} in your profile."
    context = {"report": report, "days": days, "scope": scope, "message": message}
    context |= {"sort": "unlock", "dir": "desc"}
    return web_templating.templates.TemplateResponse(request, "skills.html", context)


@router.get("/profile", response_class=fastapi_responses.HTMLResponse)
def profile_page(
    settings: web_deps.SettingsDep, request: fastapi.Request
) -> fastapi_responses.HTMLResponse:
    profile = profile_stack.load_profile(settings.profile) or profile_stack.Profile()
    context = {
        "profile": dataclasses.asdict(profile),
        "lists": profile_stack.LISTS,
        "path": settings.profile,
    }
    context["message"] = None
    return web_templating.templates.TemplateResponse(request, "profile.html", context)


@router.post("/profile", response_class=fastapi_responses.HTMLResponse)
def save_profile_page(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
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
    asked = local_analysis.recheck_stack(session)
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
    return web_templating.templates.TemplateResponse(request, "profile.html", context)


@router.post("/settings/apply", response_class=fastapi_responses.HTMLResponse)
def apply_settings(
    settings: web_deps.SettingsDep, request: fastapi.Request, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    moved = review_auto_ignore.apply_to_review(session)
    word = "vacancy" if moved == 1 else "vacancies"
    context = web_context.settings_context(session, settings)
    context["message"] = f"{moved} {word} auto-ignored from To review."
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings/linkedin", response_class=fastapi_responses.HTMLResponse)
def save_linkedin_settings(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
    pause_from: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 10,
    pause_to: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 40,
) -> fastapi_responses.HTMLResponse:
    if pause_from > pause_to:
        raise fastapi.HTTPException(status_code=422, detail="From must not be more than To")
    linkedin_public.save_pause(session, linkedin_public.PauseSettings(pause_from, pause_to))
    context = web_context.settings_context(session, settings) | {"message": "LinkedIn pause saved."}
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings/chrome-fill", response_class=fastapi_responses.HTMLResponse)
def save_chrome_fill_settings(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
    chrome_from: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 2,
    chrome_to: t.Annotated[float, fastapi.Form(ge=0, le=600)] = 4,
) -> fastapi_responses.HTMLResponse:
    if chrome_from > chrome_to:
        raise fastapi.HTTPException(status_code=422, detail="From must not be more than To")
    linkedin_chrome.save_chrome_pause(
        session, linkedin_public.PauseSettings(chrome_from, chrome_to)
    )
    context = web_context.settings_context(session, settings) | {
        "message": "Chrome fill pause saved."
    }
    return web_templating.templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings/capture", response_class=fastapi_responses.HTMLResponse)
def save_capture_settings(
    settings: web_deps.SettingsDep,
    request: fastapi.Request,
    session: web_deps.SessionDep,
    close_after: t.Annotated[float, fastapi.Form(ge=0, le=60)] = web_context.CLOSE_AFTER,
) -> fastapi_responses.HTMLResponse:
    web_context.save_close_after(session, close_after)
    context = web_context.settings_context(session, settings) | {
        "message": "Capture setting saved."
    }
    return web_templating.templates.TemplateResponse(request, "settings.html", context)
