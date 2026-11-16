"""What the pages show: filters, tabs and the context of the queue, a row and a panel.

Usage:
    import job_radar.web.context as web_context
"""

import dataclasses
import datetime as dt
import urllib.parse as urllib_parse

import fastapi
import fastapi.responses as fastapi_responses
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.prompt as claude_prompt
import job_radar.config as config
import job_radar.history.contacts as history_contacts
import job_radar.history.rejections as history_rejections
import job_radar.models as models
import job_radar.profile.stack as profile_stack
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.review.notes as review_notes
import job_radar.review.suggestions as review_suggestions
import job_radar.scoring.description as scoring_description
import job_radar.scoring.findings as scoring_findings
import job_radar.scoring.language as scoring_language
import job_radar.sources.linkedin.chrome as linkedin_chrome
import job_radar.sources.linkedin.public as linkedin_public
import job_radar.web.templating as web_templating

HISTORY_SHOWN = 8

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


def valid_tab(value: str | None) -> str:
    return value if value in TABS else DEFAULT_TAB


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


def get_opportunity(session: sa_orm.Session, opportunity_id: int) -> models.Opportunity:
    opportunity = session.get(models.Opportunity, opportunity_id)
    if opportunity is None:
        raise fastapi.HTTPException(status_code=404, detail="Opportunity not found")
    return opportunity


def panel_response(
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
    rejected = history_rejections.latest_rejections(session)

    def visible(o: models.Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = row_context(session, opportunity, rejected) | claude_context(session, opportunity.id)
    context |= tabs_context(session, tab, filters) | {"mode": "panel"}
    company = session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.company_id == opportunity.company_id)
    ).all()
    context["items_shown"] = [row_context(session, o, rejected) for o in company if visible(o)]
    context["items_gone"] = [o.id for o in company if not visible(o)]
    return web_templating.templates.TemplateResponse(request, "_panel_and_items.html", context)


def company_rows(
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
    rejected = history_rejections.latest_rejections(session)

    def visible(o: models.Opportunity) -> bool:
        in_tab = states is None or o.state in states
        return in_tab and filters.matches_opportunity(o)

    context = row_context(session, opportunity, rejected) | tabs_context(session, tab, filters)
    context["show_row"] = visible(opportunity)
    others = session.scalars(
        sa.select(models.Opportunity).where(
            models.Opportunity.company_id == opportunity.company_id,
            models.Opportunity.id != opportunity.id,
        )
    ).all()
    context["company_rows"] = [row_context(session, o, rejected) for o in others if visible(o)]
    return web_templating.templates.TemplateResponse(request, "_row_and_tabs.html", context)


def claude_context(session: sa_orm.Session, opportunity_id: int) -> dict:
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


def settings_context(session: sa_orm.Session, settings: config.Settings) -> dict:
    return {
        "rules": review_auto_ignore.load_rules(session),
        "claude_kinds": review_auto_ignore.CLAUDE_KINDS,
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
    eff_score = history_rejections.effective_score(
        opportunity.score, rejected=rejected_at is not None
    )
    extra_reasons = []
    description = scoring_description.latest_description(opportunity)
    effects = (
        review_notes.location_penalty(session, opportunity),
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


def row_context(
    session: sa_orm.Session, opportunity: models.Opportunity, rejected: dict | None = None
) -> dict:
    if rejected is None:
        rejected = history_rejections.latest_rejections(session)
    rejected_at = rejected.get(opportunity.company_id)
    history = history_contacts.company_history(session, opportunity.company_id)
    eff_score, extra_reasons, description = _score(session, opportunity, rejected_at)
    python_check = scoring_description.check_python(description) if description else None
    others = history_contacts.other_vacancies(session, opportunity.company_id, opportunity.id)
    applied = [e.at for e in history if e.kind == "applied"]
    applied += [at for o, at in others if o.state is models.OpportunityState.APPLIED]
    return {
        "history": history[:HISTORY_SHOWN],
        "history_count": _visible_history_count(history),
        "others": others,
        "applied_before": max(applied) if applied else None,
        "o": opportunity,
        "states": manual_states(),
        "rejected_at": rejected_at,
        "eff_score": eff_score,
        "extra_reasons": extra_reasons,
        "python_quote": python_check.quote if python_check else "",
        "description": description or "",
        "claude": claude_analysis.latest(session, opportunity.id),
        "chat_count": len(claude_analysis.messages(session, opportunity.id)),
        "suggestions": review_suggestions.suggest(
            opportunity,
            description=description,
            analysis=scoring_findings.latest_analysis(opportunity),
            profile=_profile(session),
            claude=claude_analysis.latest(session, opportunity.id),
        ),
        "stack_hint": _first_gap(scoring_findings.latest_analysis(opportunity), _profile(session)),
        "country": review_notes.country_from_location(opportunity.location) or "",
        "reason_kinds": [k for k in models.NoteKind if k is not models.NoteKind.NOTE],
    }


def tabs_context(session: sa_orm.Session, tab: str, filters: Filters | None = None) -> dict:
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


def queue_context(
    session: sa_orm.Session, tab: str = DEFAULT_TAB, filters: Filters | None = None, page: int = 1
) -> dict:
    filters = filters or Filters()
    rejected = history_rejections.latest_rejections(session)
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
                k["last_action"][0] if k["last_action"] else utc(k["o"].created_at),
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
        row_context(session, k["o"], rejected) | {"last_action": k.get("last_action")}
        for k in shown
    ]
    context = tabs_context(session, tab, filters)
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
        "states": manual_states(),
        "claude_status": claude_analysis.status(session),
        "added": None,
    }


def utc(value: dt.datetime) -> dt.datetime:
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
        at = utc(at)
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


def manual_states() -> list[models.OpportunityState]:
    automatic = (models.OpportunityState.NEW, models.OpportunityState.AUTO_IGNORE)
    return [s for s in models.OpportunityState if s not in automatic]
