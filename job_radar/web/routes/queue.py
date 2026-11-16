"""Routes of the review queue: the list, one vacancy, its state, notes and events.

Usage:
    import job_radar.web.routes.queue as routes_queue
"""

import datetime as dt
import typing as t

import fastapi
import fastapi.responses as fastapi_responses
import markupsafe
import sqlalchemy.orm as sa_orm

import job_radar.history.contacts as history_contacts
import job_radar.history.rejections as history_rejections
import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.review.notes as review_notes
import job_radar.sources.manual as sources_manual
import job_radar.web.context as web_context
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()


@router.get("/", response_class=fastapi_responses.HTMLResponse)
def queue(
    request: fastapi.Request,
    session: web_deps.SessionDep,
    tab: str = web_context.DEFAULT_TAB,
    country: str = "",
    company: str = "",
    title: str = "",
    q: str = "",
    desc: str = "",
    per: str = "",
    sort: str = "",
    page: int = 1,
) -> fastapi_responses.HTMLResponse:
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    return web_templating.templates.TemplateResponse(
        request,
        "queue.html",
        web_context.queue_context(session, web_context.valid_tab(tab), filters, page),
    )


@router.post("/opportunities/{opportunity_id}/state", response_class=fastapi_responses.HTMLResponse)
def set_state(
    request: fastapi.Request,
    opportunity_id: int,
    state: t.Annotated[models.OpportunityState, fastapi.Form()],
    session: web_deps.SessionDep,
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
    country: t.Annotated[str, fastapi.Form()] = "",
    company: t.Annotated[str, fastapi.Form()] = "",
    title: t.Annotated[str, fastapi.Form()] = "",
    q: t.Annotated[str, fastapi.Form()] = "",
    desc: t.Annotated[str, fastapi.Form()] = "",
    per: t.Annotated[str, fastapi.Form()] = "",
    sort: t.Annotated[str, fastapi.Form()] = "",
    source: t.Annotated[str, fastapi.Form(alias="from")] = "",
) -> fastapi_responses.HTMLResponse:
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    tab = web_context.valid_tab(tab)
    opportunity = web_context.get_opportunity(session, opportunity_id)
    opportunity.state = state
    opportunity.state_changed_at = dt.datetime.now(dt.UTC)
    # your choice replaces the rule's
    opportunity.auto_ignore_reason = None
    session.commit()
    # the vacancy page: redraw its buttons only
    if source == "page":
        context = {"o": opportunity, "states": web_context.manual_states()}
        return web_templating.templates.TemplateResponse(request, "_page_state.html", context)
    # the inbox panel (#158)
    if source == "panel":
        return web_context.panel_response(request, session, opportunity, tab, filters)
    # Replace only this row (or remove it when it leaves the tab) and refresh the tab counts
    # out of band: re-rendering the whole queue made the page jump to the end (#70).
    states = web_context.TABS[tab][1]
    context = web_context.row_context(session, opportunity) | web_context.tabs_context(
        session, tab, filters
    )
    context["show_row"] = states is None or opportunity.state in states
    return web_templating.templates.TemplateResponse(request, "_row_and_tabs.html", context)


@router.post("/opportunities/{opportunity_id}/unavailable/clear")
def clear_unavailable_mark(
    opportunity_id: int, session: web_deps.SessionDep
) -> fastapi_responses.RedirectResponse:
    # The cross on the "unavailable" badge: the next Chrome fill may try it again (#276).
    opportunity = web_context.get_opportunity(session, opportunity_id)
    review_auto_ignore.clear_unavailable(opportunity)
    session.commit()
    return fastapi_responses.RedirectResponse(f"/opportunities/{opportunity.id}", status_code=303)


@router.get("/opportunities/{opportunity_id}/row", response_class=fastapi_responses.HTMLResponse)
def opportunity_row(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    tab: str = web_context.DEFAULT_TAB,
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
    tab, filters = (
        web_context.valid_tab(tab),
        web_context.Filters(country, company, title, q, desc, per, sort),
    )
    opportunity = web_context.get_opportunity(session, opportunity_id)
    states = web_context.TABS[tab][1]
    context = web_context.row_context(session, opportunity) | web_context.tabs_context(
        session, tab, filters
    )
    in_tab = states is None or opportunity.state in states
    context["show_row"] = in_tab and filters.matches_opportunity(opportunity)
    # After a capture: say where a vacancy went when it left this tab (#162).
    if notify and not in_tab:
        context["moved_to"] = next(
            label
            for label, states in web_context.TABS.values()
            if states and opportunity.state in states
        )
    return web_templating.templates.TemplateResponse(request, "_row_and_tabs.html", context)


@router.get("/opportunities/{opportunity_id}/panel", response_class=fastapi_responses.HTMLResponse)
def opportunity_panel(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    tab: str = web_context.DEFAULT_TAB,
) -> fastapi_responses.HTMLResponse:
    # The inbox's right-hand panel (#158): the vacancy page's content.
    opportunity = web_context.get_opportunity(session, opportunity_id)
    context = web_context.row_context(session, opportunity) | web_context.claude_context(
        session, opportunity_id
    )
    context |= {"tab": web_context.valid_tab(tab), "mode": "panel"}
    return web_templating.templates.TemplateResponse(request, "_detail.html", context)


@router.get("/opportunities/{opportunity_id}", response_class=fastapi_responses.HTMLResponse)
def opportunity_page(
    request: fastapi.Request, opportunity_id: int, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    opportunity = web_context.get_opportunity(session, opportunity_id)
    context = web_context.row_context(session, opportunity) | web_context.claude_context(
        session, opportunity_id
    )
    context |= {"tab": web_context.DEFAULT_TAB, "mode": "page"}
    return web_templating.templates.TemplateResponse(request, "opportunity.html", context)


@router.post(
    "/opportunities/{opportunity_id}/rejected", response_class=fastapi_responses.HTMLResponse
)
def set_rejected(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
    country: t.Annotated[str, fastapi.Form()] = "",
    company: t.Annotated[str, fastapi.Form()] = "",
    title: t.Annotated[str, fastapi.Form()] = "",
    q: t.Annotated[str, fastapi.Form()] = "",
    desc: t.Annotated[str, fastapi.Form()] = "",
    per: t.Annotated[str, fastapi.Form()] = "",
    sort: t.Annotated[str, fastapi.Form()] = "",
    source: t.Annotated[str, fastapi.Form(alias="from")] = "",
) -> fastapi_responses.HTMLResponse:
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    # Rejection is per company: redraw this row and its company's other rows only (#138).
    opportunity = web_context.get_opportunity(session, opportunity_id)
    history_rejections.toggle_manual_rejection(session, opportunity)
    session.commit()
    if source == "panel":
        return web_context.panel_response(
            request, session, opportunity, web_context.valid_tab(tab), filters
        )
    return web_context.company_rows(
        request, session, opportunity, web_context.valid_tab(tab), filters
    )


@router.post("/opportunities/{opportunity_id}/notes", response_class=fastapi_responses.HTMLResponse)
def add_opportunity_note(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    action: t.Annotated[str, fastapi.Form()],
    kind: t.Annotated[models.NoteKind, fastapi.Form()] = models.NoteKind.NOTE,
    detail: t.Annotated[str, fastapi.Form()] = "",
    comment: t.Annotated[str, fastapi.Form()] = "",
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
    country: t.Annotated[str, fastapi.Form()] = "",
    company: t.Annotated[str, fastapi.Form()] = "",
    title: t.Annotated[str, fastapi.Form()] = "",
    q: t.Annotated[str, fastapi.Form()] = "",
    desc: t.Annotated[str, fastapi.Form()] = "",
    per: t.Annotated[str, fastapi.Form()] = "",
    sort: t.Annotated[str, fastapi.Form()] = "",
    source: t.Annotated[str, fastapi.Form(alias="from")] = "",
) -> fastapi_responses.HTMLResponse:
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    # Penalties and history are per company, so the whole queue is re-rendered.
    opportunity = web_context.get_opportunity(session, opportunity_id)
    try:
        if action == "ignore":
            review_notes.ignore_with_reason(session, opportunity, kind, detail, comment)
        elif action == "note":
            review_notes.add_note(session, opportunity, models.NoteKind.NOTE, comment=comment)
        else:
            raise fastapi.HTTPException(status_code=422, detail="Unknown action")
    except ValueError as e:
        raise fastapi.HTTPException(status_code=422, detail=str(e)) from e
    # the vacancy page: show the new state
    if source == "page":
        context = {"o": opportunity, "states": web_context.manual_states()}
        return web_templating.templates.TemplateResponse(request, "_page_state.html", context)
    if source == "panel":
        return web_context.panel_response(
            request, session, opportunity, web_context.valid_tab(tab), filters
        )
    return web_context.company_rows(
        request, session, opportunity, web_context.valid_tab(tab), filters
    )


@router.post("/notes/{note_id}/delete", response_class=fastapi_responses.HTMLResponse)
def remove_note(
    request: fastapi.Request,
    note_id: int,
    session: web_deps.SessionDep,
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
    country: t.Annotated[str, fastapi.Form()] = "",
    company: t.Annotated[str, fastapi.Form()] = "",
    title: t.Annotated[str, fastapi.Form()] = "",
    q: t.Annotated[str, fastapi.Form()] = "",
    desc: t.Annotated[str, fastapi.Form()] = "",
    per: t.Annotated[str, fastapi.Form()] = "",
    sort: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    note = session.get(models.OpportunityNote, note_id)
    if note is None:
        raise fastapi.HTTPException(status_code=404, detail="Note not found")
    opportunity = web_context.get_opportunity(session, note.opportunity_id)
    review_notes.delete_note(session, note_id)
    return web_context.company_rows(
        request, session, opportunity, web_context.valid_tab(tab), filters
    )


def _after_manual(
    request: fastapi.Request,
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    source: str,
    tab: str,
    filters: web_context.Filters,
) -> fastapi_responses.HTMLResponse:
    if source == "panel":
        return web_context.panel_response(
            request, session, opportunity, web_context.valid_tab(tab), filters
        )
    # The vacancy page: reload it, the history and the state changed.
    return fastapi_responses.HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post(
    "/opportunities/{opportunity_id}/events", response_class=fastapi_responses.HTMLResponse
)
def add_opportunity_event(
    request: fastapi.Request,
    opportunity_id: int,
    session: web_deps.SessionDep,
    kind: t.Annotated[str, fastapi.Form()],
    at: t.Annotated[str, fastapi.Form()] = "",
    comment: t.Annotated[str, fastapi.Form()] = "",
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
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
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    opportunity = web_context.get_opportunity(session, opportunity_id)
    try:
        day = dt.datetime.strptime(at, "%Y-%m-%d") if at else dt.datetime.now()
        # the date you picked, local noon
        when = day.replace(hour=12).astimezone(dt.UTC)
        sources_manual.add_event(session, opportunity, kind, when, comment)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=422, detail=str(e)) from e
    return _after_manual(request, session, opportunity, source, tab, filters)


@router.post("/history/{kind}/{item_id}/delete", response_class=fastapi_responses.HTMLResponse)
def remove_history_item(
    request: fastapi.Request,
    kind: str,
    item_id: int,
    session: web_deps.SessionDep,
    opportunity: t.Annotated[int, fastapi.Form()],
    tab: t.Annotated[str, fastapi.Form()] = web_context.DEFAULT_TAB,
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
    filters = web_context.Filters(country, company, title, q, desc, per, sort)
    shown = web_context.get_opportunity(session, opportunity)
    if not history_contacts.delete_history_item(session, kind, item_id):
        raise fastapi.HTTPException(status_code=404, detail="No such history record")
    return _after_manual(request, session, shown, source, tab, filters)


@router.post("/opportunities", response_class=fastapi_responses.HTMLResponse)
def add_vacancy(
    request: fastapi.Request,
    session: web_deps.SessionDep,
    title: t.Annotated[str, fastapi.Form()] = "",
    company: t.Annotated[str, fastapi.Form()] = "",
    link: t.Annotated[str, fastapi.Form()] = "",
    location: t.Annotated[str, fastapi.Form()] = "",
    state: t.Annotated[str, fastapi.Form()] = "new",
    comment: t.Annotated[str, fastapi.Form()] = "",
) -> fastapi_responses.HTMLResponse:
    # A vacancy found outside the sources (#182).
    try:
        opportunity = sources_manual.add_vacancy(
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
