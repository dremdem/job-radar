"""The sync pipeline, shared by the CLI (`job-radar sync gmail`) and the web UI's Sync button.

Every step reports through ``echo`` (one line per call), so the same text reaches the
terminal or the web status. See docs/queue.md#sync-mail.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from .config import Settings
from .db import make_engine, make_session_factory, upgrade_database

if TYPE_CHECKING:
    from .linkedin.public import DescriptionStats

Echo = Callable[[str], None]


def import_linkedin(session: Session, echo: Echo) -> None:
    from .linkedin.alerts import import_alerts

    stats = import_alerts(session)
    echo(f"linkedin: {stats.emails} alert emails, {stats.jobs} jobs, {stats.new} new")


def import_linkedin_search(session: Session, settings: Settings, echo: Echo) -> None:
    from .linkedin.public import PublicSearchClient, import_public_search, load_pause

    if not settings.linkedin_public_search:
        echo(
            "linkedin-search: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true to enable "
            "(see docs/linkedin-public-search.md)."
        )
        return
    pause = load_pause(session)
    stats = import_public_search(
        session,
        PublicSearchClient(pause=(pause.pause_from, pause.pause_to)),
        max_searches=settings.linkedin_public_max_searches_per_day,
        max_jobs=settings.linkedin_public_max_jobs,
    )
    echo(
        f"linkedin-search: {stats.searches} searches, {stats.requests} requests, "
        f"{stats.jobs} jobs, {stats.new} new" + (f", {stats.errors} errors" if stats.errors else "")
    )
    if stats.blocked:
        echo("linkedin-search: LinkedIn is blocking guest requests; stopped until tomorrow.")


DESCRIPTIONS_OFF = (
    "descriptions: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true to enable "
    "(see docs/linkedin-public-search.md)."
)


def fetch_descriptions(
    session: Session,
    settings: Settings,
    *,
    stop: threading.Event | None = None,
    on_wait: Callable[[float], None] | None = None,
    on_progress: "Callable[[DescriptionStats], None] | None" = None,
) -> "DescriptionStats":
    """Descriptions from LinkedIn's public job pages, with the pause from Settings (#205)."""
    from .linkedin.public import PublicSearchClient, import_public_descriptions, load_pause

    pause = load_pause(session)
    client = PublicSearchClient(
        pause=(pause.pause_from, pause.pause_to), stop=stop, on_wait=on_wait
    )
    return import_public_descriptions(
        session,
        client,
        max_per_day=settings.linkedin_public_max_descriptions_per_day,
        on_progress=on_progress,
    )


def chrome_fill_job(settings: Settings, job) -> None:
    """The Chrome fill button's background run (#247): descriptions read in your own Chrome."""
    import asyncio
    import os
    from dataclasses import asdict

    from .linkedin import chrome

    env = dict(os.environ)
    if settings.playwright_mcp_extension_token:
        env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = settings.playwright_mcp_extension_token
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            pause = chrome.load_chrome_pause(session)  # its own, not the guest one (#252)

            async def fill(browser):
                return await chrome.fill_descriptions(
                    session,
                    browser,
                    pause=(pause.pause_from, pause.pause_to),
                    stop=job.stop_event,
                    max_per_run=settings.linkedin_chrome_max_per_run,
                    on_wait=job.waiting,
                    on_progress=lambda s: job.progress(
                        total=s.total,
                        requests=s.requests,
                        saved=s.saved,
                        errors=s.errors,
                        sources=[asdict(p) for p in s.sources.values()],
                    ),
                    echo=job.echo,
                )

            command = settings.linkedin_chrome_command
            stats = asyncio.run(chrome.run_with_chrome(command, env, fill))
            job.finish(blocked=stats.blocked, reason=stats.reason, stopped=stats.stopped)
    finally:
        engine.dispose()


def import_descriptions(session: Session, settings: Settings, echo: Echo) -> None:
    """Inline, for `./jr sync` and `job-radar import descriptions`."""
    if not settings.linkedin_public_search:
        echo(DESCRIPTIONS_OFF)
        return
    stats = fetch_descriptions(session, settings)
    echo(
        f"descriptions: {stats.requests} requests, {stats.saved} saved"
        + (f", {stats.errors} errors" if stats.errors else "")
    )
    if stats.blocked:
        echo(f"descriptions: LinkedIn is blocking guest requests ({stats.reason}); "
             "stopped until tomorrow.")  # fmt: skip


def description_job(settings: Settings, job) -> None:
    """The web UI's background Descriptions job (web/description_job.py): fetch, then the
    local AI analysis on what arrived."""
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            stats = fetch_descriptions(
                session,
                settings,
                stop=job.stop_event,
                on_wait=job.waiting,
                on_progress=lambda s: job.progress(
                    total=s.total, requests=s.requests, saved=s.saved, errors=s.errors
                ),
            )
            job.finish(
                blocked=stats.blocked,
                reason=stats.reason,
                stopped=stats.stopped,
                limit_reached=stats.limit_reached,
            )
            if stats.saved and settings.ai_analysis:
                analyze_descriptions(session, settings, job.echo)
    finally:
        engine.dispose()


def import_telegram(session: Session, settings: Settings, echo: Echo) -> None:
    """New posts from the allowlisted channels via telegram-mcp (docs/telegram.md)."""
    import asyncio
    from datetime import UTC, datetime

    from .telegram.digest import TelegraphClient, import_digest
    from .telegram.mcp_reader import fetch_posts
    from .telegram.sources import load_sources
    from .telegram.sync import TelegramStats, import_posts, plan_reads

    if not settings.telegram_mcp_command:
        echo("telegram: not configured; set JOB_RADAR_TELEGRAM_MCP_COMMAND (see docs/telegram.md).")
        return
    sources = load_sources(settings.telegram_sources)
    if not sources:
        echo(f"telegram: no channels in {settings.telegram_sources} (see telegram.example.yaml).")
        return
    now = datetime.now(UTC)
    plans = plan_reads(session, sources, now=now, first_run_days=settings.telegram_first_run_days)
    try:
        results = asyncio.run(fetch_posts(settings.telegram_mcp_command, plans))
    except Exception as e:  # server missing, login expired, ...: report, keep the rest of the sync
        echo(f"telegram: could not read via telegram-mcp: {e}")
        return
    total = TelegramStats()
    telegraph = TelegraphClient()
    for plan, result in results:
        where = plan.source.name + (f" #{plan.topic_id}" if plan.topic_id else "")
        if isinstance(result, Exception):
            echo(f"telegram: {where}: failed: {result}")
            continue
        if plan.source.parse == "graph_org_digest":
            digest = import_digest(session, plan.source, result, telegraph, now=now)
            total.add(TelegramStats(posts=digest.posts, vacancies=digest.vacancies))
            echo(
                f"telegram: {where}: {digest.posts} new digests, {digest.vacancies} vacancies, "
                f"{digest.linked} matched LinkedIn vacancies"
                + (f", {digest.errors} pages not read" if digest.errors else "")
            )
            continue
        stats = import_posts(session, plan.source, plan.topic_id, result, now=now)
        total.add(stats)
        echo(f"telegram: {where}: {stats.posts} new posts, {stats.vacancies} vacancies")
    echo(f"telegram: {total.posts} new posts, {total.vacancies} vacancies")


def import_himalayas(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    """Fresh worldwide Python jobs from the Himalayas public API (docs/himalayas.md)."""
    from .himalayas import HimalayasClient
    from .himalayas import import_himalayas as run

    if not settings.himalayas:
        echo("himalayas: disabled (JOB_RADAR_HIMALAYAS=false).")
        return
    stats = run(
        session, client or HimalayasClient(), first_run_days=settings.himalayas_first_run_days
    )
    echo(
        f"himalayas: {stats.pages} pages, {stats.jobs} fresh jobs, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped as noise"
    )
    if stats.blocked:
        echo("himalayas: rate limited (429); the rest is read on the next sync.")
    if stats.error:
        echo(f"himalayas: could not read the API: {stats.error}")


def import_boards(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    """Remotive, Working Nomads, We Work Remotely (docs/boards.md)."""
    from .boards import HttpClient
    from .boards import import_boards as run

    if not settings.boards:
        echo("boards: disabled (JOB_RADAR_BOARDS=false).")
        return
    results = run(session, client or HttpClient())
    for s in results:
        if s.waited:
            echo(f"boards: {s.label}: read less than 6 h ago, skipped")
        elif s.error:
            echo(f"boards: {s.label}: could not read the feed: {s.error}")
        else:
            echo(
                f"boards: {s.label}: {s.jobs} jobs, {s.new} new, {s.known} already stored, "
                f"{s.skipped} skipped as noise, {s.old} older than 30 days"
            )
    echo(f"boards: {sum(s.new for s in results)} new")


def import_jobicy(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    """Fresh Python jobs from the Jobicy API (docs/jobicy.md)."""
    from .jobicy import JobicyClient
    from .jobicy import import_jobicy as run

    if not settings.jobicy:
        echo("jobicy: disabled (JOB_RADAR_JOBICY=false).")
        return
    stats = run(session, client or JobicyClient())
    echo(
        f"jobicy: {stats.pages} pages, {stats.jobs} fresh jobs, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped as noise"
    )
    if stats.error:
        echo(f"jobicy: could not read the API: {stats.error}")


def import_remoteok(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    """Fresh Python jobs from the Remote OK feed (docs/remoteok.md)."""
    from .remoteok import RemoteOKClient
    from .remoteok import import_remoteok as run

    if not settings.remoteok:
        echo("remoteok: disabled (JOB_RADAR_REMOTEOK=false).")
        return
    stats = run(session, client or RemoteOKClient())
    if stats.error:
        echo(f"remoteok: could not read the feed: {stats.error}")
        return
    echo(
        f"remoteok: {stats.jobs} jobs, {stats.new} new, {stats.known} already stored, "
        f"{stats.skipped} skipped as noise, {stats.old} older than 30 days"
    )


def import_hackernews(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    """Remote Python posts from "Ask HN: Who is hiring?" (docs/hackernews.md)."""
    from .hackernews import HackerNewsClient
    from .hackernews import import_hackernews as run

    if not settings.hackernews:
        echo("hackernews: disabled (JOB_RADAR_HACKERNEWS=false).")
        return
    stats = run(session, client or HackerNewsClient())
    if stats.error:
        echo(f"hackernews: could not read the thread: {stats.error}")
        return
    echo(
        f"hackernews: {stats.thread}: {stats.posts} posts, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped (not remote Python)"
    )


def analyze_descriptions(session: Session, settings: Settings, echo: Echo, model=None) -> None:
    """Work mode and residence from descriptions with a local model (docs/ai.md)."""
    from .ai.analysis import analyze_pending
    from .ai.ollama import OllamaClient, OllamaError
    from .stack import load_profile

    if not settings.ai_analysis:
        echo("ai: disabled (JOB_RADAR_AI_ANALYSIS=false).")
        return
    if model is None:
        model = OllamaClient(settings.ollama_url, settings.ai_model)
        try:
            model.check()
        except OllamaError as e:
            echo(f"ai: skipped: {e}")
            return
    profile = load_profile(settings.profile)
    if profile is None:
        echo(f"ai: no {settings.profile}; the stack check is skipped (see profile.example.yaml).")
    stats = analyze_pending(session, model, limit=settings.ai_max_per_sync, profile=profile)
    echo(
        f"ai: {stats.analyzed} descriptions analyzed, {stats.flagged} flagged"
        + (f", {stats.errors} failed ({stats.last_error})" if stats.errors else "")
        + (f", {stats.pending} left for the next run" if stats.pending else "")
    )


def gmail_client_or_none(settings: Settings):
    from google.auth.exceptions import RefreshError

    from .gmail.auth import GmailAuthError, load_credentials
    from .gmail.client import ApiGmailClient

    try:
        return ApiGmailClient(load_credentials(settings.gmail_token))
    except (GmailAuthError, RefreshError):  # no token, or it expired: work without Gmail
        return None


def import_rejections(
    session: Session,
    settings: Settings,
    echo: Echo,
    client=None,
    *,
    recheck: bool = False,
    use_gmail: bool = True,
) -> None:
    from .gmail.client import body_fetcher
    from .linkedin.rejections import import_rejections as run

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    stats = run(session, fetch_body=body_fetcher(client) if client else None, recheck=recheck)
    echo(
        f"rejections: {stats.emails} emails, {stats.detected} rejections, {stats.new} new"
        + (f", {stats.fetched} bodies checked in Gmail" if stats.fetched else "")
    )
    if stats.pending:
        echo(
            f"rejections: {stats.pending} without body wait for Gmail access; "
            "run `job-radar gmail auth`, then this command again."
        )


def import_applications(
    session: Session, settings: Settings, echo: Echo, client=None, *, use_gmail: bool = True
) -> None:
    from .applications import import_applications as run
    from .gmail.client import body_fetcher

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    stats = run(session, fetch_body=body_fetcher(client) if client else None)
    echo(
        f"applications: {stats.detected} found, {stats.new} new, "
        f"{stats.linked} linked to an existing one"
        + (f", {stats.fetched} bodies checked in Gmail" if stats.fetched else "")
    )
    if stats.pending:
        echo(
            f"applications: role unknown for {stats.pending} LinkedIn confirmations until "
            "Gmail access; run `job-radar gmail auth`, then this command again."
        )


def import_hh(
    session: Session, settings: Settings, echo: Echo, client=None, *, use_gmail: bool = True
) -> None:
    """Rejections, applications and employer messages from hh.ru mail (docs/hh.md)."""
    from .gmail.client import body_fetcher
    from .hh.digests import import_hh_digests
    from .hh.notifications import import_hh_notifications

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    fetch = body_fetcher(client) if client else None
    stats = import_hh_notifications(session, fetch)
    echo(
        f"hh: {stats.notices} notices, {stats.rejections} rejections, "
        f"{stats.vacancies} applied vacancies, {stats.messages} employer messages"
        + (f", {stats.pending} wait for Gmail access" if stats.pending else "")
    )
    if settings.hh_digests_since is None:
        echo("hh digests: off; set JOB_RADAR_HH_DIGESTS_SINCE (see docs/hh.md).")
        return
    digests = import_hh_digests(session, fetch, since=settings.hh_digests_since)
    echo(
        f"hh digests: {digests.digests} digests, {digests.vacancies} new vacancies, "
        f"{digests.known} already known, {digests.skipped} skipped as noise"
        + (f", {digests.pending} wait for Gmail access" if digests.pending else "")
    )


def import_contacts(session: Session, echo: Echo) -> None:
    from .contacts import link_and_label

    stats = link_and_label(session)
    linked = stats.by_thread + stats.by_domain
    echo(
        f"contacts: {linked} linked ({stats.by_thread} by thread, {stats.by_domain} by domain), "
        f"{stats.probable} probable by name, {stats.labeled} labeled, "
        f"{stats.rejections} rejections from other mail"
    )


@dataclass
class Step:
    """One source or analysis of a sync, for the progress list (docs/queue.md#sync)."""

    key: str
    label: str
    state: str = "waiting"  # waiting / running / done / off / failed
    summary: str = ""


StepsCallback = Callable[[list[Step]], None]
# (key, label, prefix of its echo lines, which line sums it up)
STEPS: list[tuple[str, str, str, str]] = [
    ("gmail", "Gmail", "", "all"),
    ("linkedin", "LinkedIn alerts", "linkedin", "first"),
    ("linkedin_search", "LinkedIn search", "linkedin-search", "first"),
    ("telegram", "Telegram", "telegram", "last"),
    ("himalayas", "Himalayas", "himalayas", "first"),
    ("hackernews", "Hacker News", "hackernews", "first"),
    ("remoteok", "Remote OK", "remoteok", "first"),
    ("jobicy", "Jobicy", "jobicy", "first"),
    ("boards", "Other boards", "boards", "last"),
    ("mail", "Mail analysis", "", "all"),
    ("descriptions", "LinkedIn descriptions", "descriptions", "first"),
    ("ai", "AI analysis", "ai", "last"),
]


def describe_error(error: Exception) -> str:
    """A failed step's reason, with what to do for the usual ones."""
    from google.auth.exceptions import RefreshError

    from .gmail.auth import GmailAuthError

    if isinstance(error, RefreshError):
        return "Gmail access expired or revoked: run `./jr auth` (docs/gmail.md)"
    if isinstance(error, GmailAuthError):
        return str(error).replace("`job-radar gmail auth`", "`./jr auth` (`job-radar gmail auth`)")
    return str(error) or error.__class__.__name__


def _summary(lines: list[str], prefix: str, pick: str) -> str:
    def strip(line: str) -> str:
        return line.removeprefix(f"{prefix}: ") if prefix else line

    if not lines:
        return ""
    if pick == "first":
        return strip(lines[0])
    if pick == "last":
        return strip(lines[-1])
    return " · ".join(strip(line) for line in lines)


def run_sync(
    settings: Settings,
    echo: Echo,
    *,
    names: list[str] | None = None,
    since: date | None = None,
    progress_echo: Echo | None = None,
    on_steps: StepsCallback | None = None,
    start_descriptions: Callable[[], bool] | None = None,
) -> list[Step]:
    """Every source and analysis, each on its own: a failed step does not stop the rest.
    ``start_descriptions``: the web UI fetches descriptions in a background job (#205)."""
    from .gmail.auth import load_credentials
    from .gmail.client import ApiGmailClient
    from .gmail.sync import sync_gmail

    progress_echo = progress_echo or echo
    enabled = {
        "linkedin_search": settings.linkedin_public_search,
        "telegram": bool(settings.telegram_mcp_command),
        "himalayas": settings.himalayas,
        "hackernews": settings.hackernews,
        "remoteok": settings.remoteok,
        "jobicy": settings.jobicy,
        "boards": settings.boards,
        "descriptions": settings.linkedin_public_search,
        "ai": settings.ai_analysis,
    }
    steps = [Step(key, label) for key, label, _, _ in STEPS]
    for step in steps:
        if not enabled.get(step.key, True):
            step.state = "off"
    by_key = {s.key: s for s in steps}
    how = {key: (prefix, pick) for key, _, prefix, pick in STEPS}

    def notify() -> None:
        if on_steps:
            on_steps([Step(**vars(s)) for s in steps])

    def run(key: str, action: Callable[[Echo], object]) -> None:
        step = by_key[key]
        if step.state == "off":
            return
        step.state = "running"
        notify()
        lines: list[str] = []

        def step_echo(line: str) -> None:
            lines.append(line)
            echo(line)

        try:
            action(step_echo)
            step.state, step.summary = "done", _summary(lines, *how[key])
        except Exception as e:  # reported on the step; the next steps still run
            step.state, step.summary = "failed", describe_error(e)
            echo(f"{key}: failed: {step.summary}")
        notify()

    notify()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    client = None
    try:
        with make_session_factory(engine)() as session:

            def gmail(step_echo: Echo) -> None:
                nonlocal client

                def progress(name: str, stats) -> None:
                    if stats.threads % 50 == 0:
                        progress_echo(f"{name}: {stats.threads} threads, {stats.new} new...")

                api = ApiGmailClient(load_credentials(settings.gmail_token))
                try:
                    results = sync_gmail(session, api, names=names, since=since, progress=progress)
                except Exception:
                    session.rollback()
                    raise
                client = api
                for name, stats in results.items():
                    step_echo(f"{name}: {stats.threads} threads, {stats.new} new")

            def mail(step_echo: Echo) -> None:
                # Without Gmail access: stored mail only, no bodies fetched.
                use = client is not None
                import_rejections(session, settings, step_echo, client, use_gmail=use)
                import_applications(session, settings, step_echo, client, use_gmail=use)
                import_hh(session, settings, step_echo, client, use_gmail=use)
                import_contacts(session, step_echo)

            run("gmail", gmail)
            run("linkedin", lambda e: import_linkedin(session, e))
            run("linkedin_search", lambda e: import_linkedin_search(session, settings, e))
            run("telegram", lambda e: import_telegram(session, settings, e))
            run("himalayas", lambda e: import_himalayas(session, settings, e))
            run("hackernews", lambda e: import_hackernews(session, settings, e))
            run("remoteok", lambda e: import_remoteok(session, settings, e))
            run("jobicy", lambda e: import_jobicy(session, settings, e))
            run("boards", lambda e: import_boards(session, settings, e))
            run("mail", mail)
            # Late: the rules for fetched descriptions see applications/rejections above.
            if start_descriptions is not None:
                run("descriptions", lambda e: e(
                    "descriptions: started in the background" if start_descriptions()
                    else "descriptions: already running"
                ))  # fmt: skip
            else:
                run("descriptions", lambda e: import_descriptions(session, settings, e))
            run("ai", lambda e: analyze_descriptions(session, settings, e))
    finally:
        engine.dispose()
    return steps
