"""The sync pipeline, shared by the CLI and the web UI's Sync button.

Every source has an ``import_*`` step: mail (LinkedIn alerts, rejections, applications,
hh.ru, contacts), job boards (Himalayas, Remotive and the like, Jobicy, Remote OK, FDE
Times, Hacker News), Telegram, original postings on employer ATS boards, and the
description jobs (public pages, Chrome fill, local model analysis). ``run_sync`` runs them
one by one; a failed step is reported and does not stop the rest. Every step reports
through ``echo`` (one line per call), so the same text reaches the terminal or the web
status. See docs/queue.md#sync-mail.

Usage:
    import job_radar.jobs.pipeline as jobs_pipeline
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import threading
import typing as t

import sqlalchemy.orm as sa_orm

import job_radar.config as config
import job_radar.db as db

if t.TYPE_CHECKING:
    import job_radar.sources.linkedin.public as linkedin_public

Echo = cabc.Callable[[str], None]


def import_linkedin(session: sa_orm.Session, echo: Echo) -> None:
    import job_radar.sources.linkedin.alerts as linkedin_alerts

    stats = linkedin_alerts.import_alerts(session)
    echo(f"linkedin: {stats.emails} alert emails, {stats.jobs} jobs, {stats.new} new")


def import_linkedin_search(session: sa_orm.Session, settings: config.Settings, echo: Echo) -> None:
    import job_radar.sources.linkedin.public as linkedin_public

    if not settings.linkedin_public_search:
        echo(
            "linkedin-search: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true to enable "
            "(see docs/linkedin-public-search.md)."
        )
        return
    pause = linkedin_public.load_pause(session)
    stats = linkedin_public.import_public_search(
        session,
        linkedin_public.PublicSearchClient(pause=(pause.pause_from, pause.pause_to)),
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
    "descriptions: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true (LinkedIn, see "
    "docs/linkedin-public-search.md) or JOB_RADAR_FDETIMES=true (docs/fdetimes.md) to enable."
)


def descriptions_on(settings: config.Settings) -> bool:
    """The Descriptions job has a source: LinkedIn's guest pages or FDE Times (docs/fdetimes.md)."""
    return settings.linkedin_public_search or settings.fdetimes


def fetch_descriptions(
    session: sa_orm.Session,
    settings: config.Settings,
    *,
    stop: threading.Event | None = None,
    on_wait: cabc.Callable[[float], None] | None = None,
    on_progress: "cabc.Callable[[linkedin_public.DescriptionStats], None] | None" = None,
    echo: Echo | None = None,
    page_get=None,
) -> "linkedin_public.DescriptionStats":
    """Descriptions from LinkedIn's public job pages, then from FDE Times role pages, each
    with the guest pause from Settings (#205). The counts are summed; ``blocked`` stays
    LinkedIn's, FDE Times' own stop goes to ``echo``. ``page_get``: tests pass a fake."""
    import job_radar.sources.boards.fdetimes as boards_fdetimes
    import job_radar.sources.linkedin.public as linkedin_public

    pause = linkedin_public.load_pause(session)
    stats = linkedin_public.DescriptionStats()
    if settings.linkedin_public_search:
        client = linkedin_public.PublicSearchClient(
            pause=(pause.pause_from, pause.pause_to), stop=stop, on_wait=on_wait
        )
        stats = linkedin_public.import_public_descriptions(
            session,
            client,
            max_per_day=settings.linkedin_public_max_descriptions_per_day,
            on_progress=on_progress,
        )
    if not settings.fdetimes or stats.stopped:
        return stats
    before = linkedin_public.DescriptionStats(**vars(stats))

    def merged(fde) -> "linkedin_public.DescriptionStats":
        out = linkedin_public.DescriptionStats(**vars(before))
        out.total += fde.total
        out.requests += fde.requests
        out.saved += fde.saved
        out.errors += fde.errors
        out.stopped = fde.stopped
        return out

    options = {"http_get": page_get} if page_get else {}
    fde = boards_fdetimes.import_descriptions(
        session,
        boards_fdetimes.PageClient(
            pause=(pause.pause_from, pause.pause_to), stop=stop, on_wait=on_wait, **options
        ),
        max_per_run=settings.fdetimes_max_descriptions,
        on_progress=(lambda s: on_progress(merged(s))) if on_progress else None,
    )
    say = echo or (lambda line: None)
    if fde.total:
        say(
            f"descriptions: FDE Times: {fde.requests} requests, {fde.saved} saved"
            + (f", {fde.gone} gone" if fde.gone else "")
            + (f", {fde.errors} errors" if fde.errors else "")
        )
    for problem in fde.problems:
        say(f"descriptions: FDE Times: {problem}")
    if fde.blocked:
        say(f"descriptions: FDE Times answered {fde.reason}; stopped until the next run.")
    return merged(fde)


def chrome_fill_job(settings: config.Settings, job) -> None:
    """The Chrome fill button's background run (#247): descriptions read in your own Chrome."""
    import asyncio
    import dataclasses
    import os

    import job_radar.sources.linkedin.chrome as linkedin_chrome

    env = dict(os.environ)
    if settings.playwright_mcp_extension_token:
        env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = settings.playwright_mcp_extension_token
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            # its own, not the guest one (#252)
            pause = linkedin_chrome.load_chrome_pause(session)

            async def fill(browser):
                return await linkedin_chrome.fill_descriptions(
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
                        sources=[dataclasses.asdict(p) for p in s.sources.values()],
                        failed=list(s.failed),
                    ),
                    echo=job.echo,
                )

            command = settings.linkedin_chrome_command
            stats = asyncio.run(linkedin_chrome.run_with_chrome(command, env, fill))
            job.finish(blocked=stats.blocked, reason=stats.reason, stopped=stats.stopped)
    finally:
        engine.dispose()


def import_descriptions(session: sa_orm.Session, settings: config.Settings, echo: Echo) -> None:
    """Inline, for `./jr sync` and `job-radar import descriptions`."""
    if not descriptions_on(settings):
        echo(DESCRIPTIONS_OFF)
        return
    # the summary line comes first: the Sync step shows it
    later: list[str] = []
    stats = fetch_descriptions(session, settings, echo=later.append)
    echo(
        f"descriptions: {stats.requests} requests, {stats.saved} saved"
        + (f", {stats.errors} errors" if stats.errors else "")
    )
    for line in later:
        echo(line)
    if stats.blocked:
        echo(f"descriptions: LinkedIn is blocking guest requests ({stats.reason}); "
             "stopped until tomorrow.")  # fmt: skip


def description_job(settings: config.Settings, job) -> None:
    """The web UI's background Descriptions job (web/description_job.py): fetch, then the
    local AI analysis on what arrived."""
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            stats = fetch_descriptions(
                session,
                settings,
                stop=job.stop_event,
                on_wait=job.waiting,
                on_progress=lambda s: job.progress(
                    total=s.total, requests=s.requests, saved=s.saved, errors=s.errors
                ),
                echo=job.echo,
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


def import_telegram(session: sa_orm.Session, settings: config.Settings, echo: Echo) -> None:
    """New posts from the allowlisted channels via telegram-mcp (docs/telegram.md)."""
    import asyncio
    import datetime as dt

    import job_radar.sources.telegram.digest as telegram_digest
    import job_radar.sources.telegram.mcp_reader as telegram_mcp_reader
    import job_radar.sources.telegram.sources as telegram_sources
    import job_radar.sources.telegram.sync as telegram_sync

    if not settings.telegram_mcp_command:
        echo("telegram: not configured; set JOB_RADAR_TELEGRAM_MCP_COMMAND (see docs/telegram.md).")
        return
    sources = telegram_sources.load_sources(settings.telegram_sources)
    if not sources:
        echo(f"telegram: no channels in {settings.telegram_sources} (see telegram.example.yaml).")
        return
    now = dt.datetime.now(dt.UTC)
    plans = telegram_sync.plan_reads(
        session, sources, now=now, first_run_days=settings.telegram_first_run_days
    )
    try:
        results = asyncio.run(telegram_mcp_reader.fetch_posts(settings.telegram_mcp_command, plans))
    except Exception as e:
        # server missing, login expired, ...: report, keep the rest of the sync
        echo(f"telegram: could not read via telegram-mcp: {e}")
        return
    total = telegram_sync.TelegramStats()
    telegraph = telegram_digest.TelegraphClient()
    for plan, result in results:
        where = plan.source.name + (f" #{plan.topic_id}" if plan.topic_id else "")
        if isinstance(result, Exception):
            echo(f"telegram: {where}: failed: {result}")
            continue
        if plan.source.parse == "graph_org_digest":
            digest = telegram_digest.import_digest(session, plan.source, result, telegraph, now=now)
            total.add(telegram_sync.TelegramStats(posts=digest.posts, vacancies=digest.vacancies))
            echo(
                f"telegram: {where}: {digest.posts} new digests, {digest.vacancies} vacancies, "
                f"{digest.linked} matched LinkedIn vacancies"
                + (f", {digest.errors} pages not read" if digest.errors else "")
            )
            continue
        stats = telegram_sync.import_posts(session, plan.source, plan.topic_id, result, now=now)
        total.add(stats)
        echo(f"telegram: {where}: {stats.posts} new posts, {stats.vacancies} vacancies")
    echo(f"telegram: {total.posts} new posts, {total.vacancies} vacancies")


def import_himalayas(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Fresh worldwide Python jobs from the Himalayas public API (docs/himalayas.md)."""
    import job_radar.sources.boards.himalayas as boards_himalayas

    if not settings.himalayas:
        echo("himalayas: disabled (JOB_RADAR_HIMALAYAS=false).")
        return
    stats = boards_himalayas.import_himalayas(
        session,
        client or boards_himalayas.HimalayasClient(),
        first_run_days=settings.himalayas_first_run_days,
    )
    echo(
        f"himalayas: {stats.pages} pages, {stats.jobs} fresh jobs, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped as noise"
    )
    if stats.blocked:
        echo("himalayas: rate limited (429); the rest is read on the next sync.")
    if stats.error:
        echo(f"himalayas: could not read the API: {stats.error}")


def import_boards(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Remotive, Working Nomads, We Work Remotely (docs/boards.md)."""
    import job_radar.sources.boards.feeds as boards_feeds

    if not settings.boards:
        echo("boards: disabled (JOB_RADAR_BOARDS=false).")
        return
    results = boards_feeds.import_boards(session, client or boards_feeds.HttpClient())
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


def import_jobicy(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Fresh Python jobs from the Jobicy API (docs/jobicy.md)."""
    import job_radar.sources.boards.jobicy as boards_jobicy

    if not settings.jobicy:
        echo("jobicy: disabled (JOB_RADAR_JOBICY=false).")
        return
    stats = boards_jobicy.import_jobicy(session, client or boards_jobicy.JobicyClient())
    echo(
        f"jobicy: {stats.pages} pages, {stats.jobs} fresh jobs, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped as noise"
    )
    if stats.error:
        echo(f"jobicy: could not read the API: {stats.error}")


def import_fdetimes(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Fresh Forward Deployed Engineer roles from FDE Times (docs/fdetimes.md)."""
    import job_radar.sources.boards.fdetimes as boards_fdetimes

    if not settings.fdetimes:
        echo("fdetimes: disabled (JOB_RADAR_FDETIMES=false).")
        return
    stats = boards_fdetimes.import_fdetimes(
        session,
        client or boards_fdetimes.FDETimesClient(),
        first_run_days=settings.fdetimes_first_run_days,
        regions=boards_fdetimes.parse_regions(settings.fdetimes_regions),
        related=settings.fdetimes_related,
    )
    if stats.error:
        echo(f"fdetimes: could not read the job list: {stats.error}")
        return
    echo(
        f"fdetimes: {stats.fresh} fresh roles, {stats.new} new, {stats.known} already stored, "
        f"{stats.skipped} skipped (kind, region or role)"
    )


def import_ats(session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None) -> None:
    """Original postings of alert vacancies on the employer's ATS board (docs/ats.md)."""
    import job_radar.sources.ats.boards as ats_boards
    import job_radar.sources.ats.resolve as ats_resolve

    if not settings.ats:
        echo("ats: disabled (JOB_RADAR_ATS=false).")
        return
    stats = ats_resolve.import_ats(
        session,
        client or ats_boards.AtsClient(max_requests=settings.ats_max_requests),
        guess=settings.ats_guess,
    )
    echo(
        f"ats: {stats.vacancies} vacancies checked, {stats.matched} matched, "
        f"{stats.ambiguous} to pick by hand, {stats.no_match} not found, "
        f"{stats.no_board} without a board, {stats.requests} requests"
    )
    if stats.taken:
        echo(f"ats: {stats.taken} postings already on another vacancy")
    for error in stats.errors[:5]:
        echo(f"ats: {error}")
    if stats.limited:
        echo("ats: request limit reached; the rest waits for the next run.")


def import_remoteok(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Fresh Python jobs from the Remote OK feed (docs/remoteok.md)."""
    import job_radar.sources.boards.remoteok as boards_remoteok

    if not settings.remoteok:
        echo("remoteok: disabled (JOB_RADAR_REMOTEOK=false).")
        return
    stats = boards_remoteok.import_remoteok(session, client or boards_remoteok.RemoteOKClient())
    if stats.error:
        echo(f"remoteok: could not read the feed: {stats.error}")
        return
    echo(
        f"remoteok: {stats.jobs} jobs, {stats.new} new, {stats.known} already stored, "
        f"{stats.skipped} skipped as noise, {stats.old} older than 30 days"
    )


def import_hackernews(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, client=None
) -> None:
    """Remote Python posts from "Ask HN: Who is hiring?" (docs/hackernews.md)."""
    import job_radar.sources.boards.hackernews as boards_hackernews

    if not settings.hackernews:
        echo("hackernews: disabled (JOB_RADAR_HACKERNEWS=false).")
        return
    stats = boards_hackernews.import_hackernews(
        session, client or boards_hackernews.HackerNewsClient()
    )
    if stats.error:
        echo(f"hackernews: could not read the thread: {stats.error}")
        return
    echo(
        f"hackernews: {stats.thread}: {stats.posts} posts, {stats.new} new, "
        f"{stats.known} already stored, {stats.skipped} skipped (not remote Python)"
    )


def analyze_descriptions(
    session: sa_orm.Session, settings: config.Settings, echo: Echo, model=None
) -> None:
    """Work mode and residence from descriptions with a local model (docs/ai.md)."""
    import job_radar.ai.local.analysis as local_analysis
    import job_radar.ai.local.ollama as local_ollama
    import job_radar.profile.stack as profile_stack

    if not settings.ai_analysis:
        echo("ai: disabled (JOB_RADAR_AI_ANALYSIS=false).")
        return
    if model is None:
        model = local_ollama.OllamaClient(settings.ollama_url, settings.ai_model)
        try:
            model.check()
        except local_ollama.OllamaError as e:
            echo(f"ai: skipped: {e}")
            return
    profile = profile_stack.load_profile(settings.profile)
    if profile is None:
        echo(f"ai: no {settings.profile}; the stack check is skipped (see profile.example.yaml).")
    stats = local_analysis.analyze_pending(
        session, model, limit=settings.ai_max_per_sync, profile=profile
    )
    echo(
        f"ai: {stats.analyzed} descriptions analyzed, {stats.flagged} flagged"
        + (f", {stats.errors} failed ({stats.last_error})" if stats.errors else "")
        + (f", {stats.pending} left for the next run" if stats.pending else "")
    )


def gmail_client_or_none(settings: config.Settings):
    import google.auth.exceptions as auth_exceptions

    import job_radar.sources.gmail.auth as gmail_auth
    import job_radar.sources.gmail.client as gmail_client

    try:
        return gmail_client.ApiGmailClient(gmail_auth.load_credentials(settings.gmail_token))
    except (gmail_auth.GmailAuthError, auth_exceptions.RefreshError):
        # no token, or it expired: work without Gmail
        return None


def import_rejections(
    session: sa_orm.Session,
    settings: config.Settings,
    echo: Echo,
    client=None,
    *,
    recheck: bool = False,
    use_gmail: bool = True,
) -> None:
    import job_radar.sources.gmail.client as gmail_client
    import job_radar.sources.linkedin.rejections as linkedin_rejections

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    stats = linkedin_rejections.import_rejections(
        session, fetch_body=gmail_client.body_fetcher(client) if client else None, recheck=recheck
    )
    echo(
        f"rejections: {stats.emails} emails, {stats.detected} rejections, {stats.new} new"
        + (f", {stats.fetched} bodies checked in Gmail" if stats.fetched else "")
        + (f", {stats.gone} gone from Gmail" if stats.gone else "")
    )
    if stats.pending:
        echo(
            f"rejections: {stats.pending} without body wait for Gmail access; "
            "run `job-radar gmail auth`, then this command again."
        )


def import_applications(
    session: sa_orm.Session,
    settings: config.Settings,
    echo: Echo,
    client=None,
    *,
    use_gmail: bool = True,
) -> None:
    import job_radar.history.applications as history_applications
    import job_radar.sources.gmail.client as gmail_client

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    stats = history_applications.import_applications(
        session, fetch_body=gmail_client.body_fetcher(client) if client else None
    )
    echo(
        f"applications: {stats.detected} found, {stats.new} new, "
        f"{stats.linked} linked to an existing one"
        + (f", {stats.fetched} bodies checked in Gmail" if stats.fetched else "")
        + (f", {stats.gone} gone from Gmail" if stats.gone else "")
    )
    if stats.pending:
        echo(
            f"applications: role unknown for {stats.pending} LinkedIn confirmations until "
            "Gmail access; run `job-radar gmail auth`, then this command again."
        )


def import_hh(
    session: sa_orm.Session,
    settings: config.Settings,
    echo: Echo,
    client=None,
    *,
    use_gmail: bool = True,
) -> None:
    """Rejections, applications and employer messages from hh.ru mail (docs/hh.md)."""
    import job_radar.sources.gmail.client as gmail_client
    import job_radar.sources.hh.digests as hh_digests
    import job_radar.sources.hh.notifications as hh_notifications

    if use_gmail:
        client = client or gmail_client_or_none(settings)
    fetch = gmail_client.body_fetcher(client) if client else None
    stats = hh_notifications.import_hh_notifications(session, fetch)
    echo(
        f"hh: {stats.notices} notices, {stats.rejections} rejections, "
        f"{stats.vacancies} applied vacancies, {stats.messages} employer messages"
        + (f", {stats.pending} wait for Gmail access" if stats.pending else "")
        + (f", {stats.gone} gone from Gmail" if stats.gone else "")
    )
    if settings.hh_digests_since is None:
        echo("hh digests: off; set JOB_RADAR_HH_DIGESTS_SINCE (see docs/hh.md).")
        return
    digests = hh_digests.import_hh_digests(session, fetch, since=settings.hh_digests_since)
    echo(
        f"hh digests: {digests.digests} digests, {digests.vacancies} new vacancies, "
        f"{digests.known} already known, {digests.skipped} skipped as noise"
        + (f", {digests.pending} wait for Gmail access" if digests.pending else "")
        + (f", {digests.gone} gone from Gmail" if digests.gone else "")
    )


def import_contacts(session: sa_orm.Session, echo: Echo) -> None:
    import job_radar.history.contacts as history_contacts

    stats = history_contacts.link_and_label(session)
    linked = stats.by_thread + stats.by_domain
    echo(
        f"contacts: {linked} linked ({stats.by_thread} by thread, {stats.by_domain} by domain), "
        f"{stats.probable} probable by name, {stats.labeled} labeled, "
        f"{stats.rejections} rejections from other mail"
    )


@dataclasses.dataclass
class Step:
    """One source or analysis of a sync, for the progress list (docs/queue.md#sync)."""

    key: str
    label: str
    # waiting / running / done / off / failed
    state: str = "waiting"
    summary: str = ""


StepsCallback = cabc.Callable[[list[Step]], None]
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
    ("fdetimes", "FDE Times", "fdetimes", "first"),
    ("boards", "Other boards", "boards", "last"),
    ("mail", "Mail analysis", "", "all"),
    ("ats", "ATS originals", "ats", "first"),
    ("descriptions", "Descriptions", "descriptions", "first"),
    ("ai", "AI analysis", "ai", "last"),
]


def describe_error(error: Exception) -> str:
    """A failed step's reason, with what to do for the usual ones."""
    import google.auth.exceptions as auth_exceptions

    import job_radar.sources.gmail.auth as gmail_auth

    if isinstance(error, auth_exceptions.RefreshError):
        return "Gmail access expired or revoked: run `./jr auth` (docs/gmail.md)"
    if isinstance(error, gmail_auth.GmailAuthError):
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
    settings: config.Settings,
    echo: Echo,
    *,
    names: list[str] | None = None,
    since: dt.date | None = None,
    progress_echo: Echo | None = None,
    on_steps: StepsCallback | None = None,
    start_descriptions: cabc.Callable[[], bool] | None = None,
) -> list[Step]:
    """Every source and analysis, each on its own: a failed step does not stop the rest.
    ``start_descriptions``: the web UI fetches descriptions in a background job (#205)."""
    import job_radar.sources.gmail.auth as gmail_auth
    import job_radar.sources.gmail.client as gmail_client
    import job_radar.sources.gmail.sync as gmail_sync

    progress_echo = progress_echo or echo
    enabled = {
        "linkedin_search": settings.linkedin_public_search,
        "telegram": bool(settings.telegram_mcp_command),
        "himalayas": settings.himalayas,
        "hackernews": settings.hackernews,
        "remoteok": settings.remoteok,
        "jobicy": settings.jobicy,
        "fdetimes": settings.fdetimes,
        "boards": settings.boards,
        "ats": settings.ats,
        "descriptions": descriptions_on(settings),
        "ai": settings.ai_analysis,
    }
    steps = [Step(key, label) for key, label, _, _ in STEPS]
    for step in steps:
        if not enabled.get(step.key, True):
            step.state = "off"
    if not settings.gmail_client_secret.exists() and not settings.gmail_token.exists():
        # Never set up (a fresh clone): not a failure, the boards still sync (#8).
        gmail_step = next(s for s in steps if s.key == "gmail")
        gmail_step.state, gmail_step.summary = "off", "not set up (docs/gmail.md)"
    by_key = {s.key: s for s in steps}
    how = {key: (prefix, pick) for key, _, prefix, pick in STEPS}

    def notify() -> None:
        if on_steps:
            on_steps([Step(**vars(s)) for s in steps])

    def run(key: str, action: cabc.Callable[[Echo], object]) -> None:
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
        except Exception as e:
            # reported on the step; the next steps still run
            step.state, step.summary = "failed", describe_error(e)
            echo(f"{key}: failed: {step.summary}")
        notify()

    notify()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    client = None
    try:
        with db.make_session_factory(engine)() as session:

            def gmail(step_echo: Echo) -> None:
                nonlocal client

                def progress(name: str, stats) -> None:
                    if stats.threads % 50 == 0:
                        progress_echo(f"{name}: {stats.threads} threads, {stats.new} new...")

                api = gmail_client.ApiGmailClient(gmail_auth.load_credentials(settings.gmail_token))
                try:
                    results = gmail_sync.sync_gmail(
                        session, api, names=names, since=since, progress=progress
                    )
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
            run("fdetimes", lambda e: import_fdetimes(session, settings, e))
            run("boards", lambda e: import_boards(session, settings, e))
            run("mail", mail)
            # After mail (applications fold copies away), before the public descriptions, which
            # skip vacancies that already have one.
            run("ats", lambda e: import_ats(session, settings, e))
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
