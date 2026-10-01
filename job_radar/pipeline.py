"""The sync pipeline, shared by the CLI (`job-radar sync gmail`) and the web UI's Sync button.

Every step reports through ``echo`` (one line per call), so the same text reaches the
terminal or the web status. See docs/queue.md#sync-mail.
"""

from collections.abc import Callable
from datetime import date

from sqlalchemy.orm import Session

from .config import Settings
from .db import make_engine, make_session_factory, upgrade_database

Echo = Callable[[str], None]


def import_linkedin(session: Session, echo: Echo) -> None:
    from .linkedin.alerts import import_alerts

    stats = import_alerts(session)
    echo(f"linkedin: {stats.emails} alert emails, {stats.jobs} jobs, {stats.new} new")


def import_linkedin_search(session: Session, settings: Settings, echo: Echo) -> None:
    from .linkedin.public import PublicSearchClient, import_public_search

    if not settings.linkedin_public_search:
        echo(
            "linkedin-search: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true to enable "
            "(see docs/linkedin-public-search.md)."
        )
        return
    stats = import_public_search(
        session,
        PublicSearchClient(delay=settings.linkedin_public_delay_seconds),
        max_searches=settings.linkedin_public_max_searches_per_day,
        max_jobs=settings.linkedin_public_max_jobs,
    )
    echo(
        f"linkedin-search: {stats.searches} searches, {stats.requests} requests, "
        f"{stats.jobs} jobs, {stats.new} new" + (f", {stats.errors} errors" if stats.errors else "")
    )
    if stats.blocked:
        echo("linkedin-search: LinkedIn is blocking guest requests; stopped until tomorrow.")


def import_descriptions(session: Session, settings: Settings, echo: Echo) -> None:
    from .linkedin.public import PublicSearchClient, import_public_descriptions

    if not settings.linkedin_public_search:
        echo(
            "descriptions: disabled; set JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true to enable "
            "(see docs/linkedin-public-search.md)."
        )
        return
    stats = import_public_descriptions(
        session,
        PublicSearchClient(delay=settings.linkedin_public_delay_seconds),
        max_per_day=settings.linkedin_public_max_descriptions_per_day,
    )
    echo(
        f"descriptions: {stats.requests} requests, {stats.saved} saved"
        + (f", {stats.errors} errors" if stats.errors else "")
    )
    if stats.blocked:
        echo("descriptions: LinkedIn is blocking guest requests; stopped until tomorrow.")


def import_telegram(session: Session, settings: Settings, echo: Echo) -> None:
    """New posts from the allowlisted channels via telegram-mcp (docs/telegram.md)."""
    import asyncio
    from datetime import UTC, datetime

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
    for plan, result in results:
        where = plan.source.name + (f" #{plan.topic_id}" if plan.topic_id else "")
        if isinstance(result, Exception):
            echo(f"telegram: {where}: failed: {result}")
            continue
        stats = import_posts(session, plan.source, plan.topic_id, result, now=now)
        total.add(stats)
        echo(f"telegram: {where}: {stats.posts} new posts, {stats.vacancies} vacancies")
    echo(f"telegram: {total.posts} new posts, {total.vacancies} vacancies")


def gmail_client_or_none(settings: Settings):
    from .gmail.auth import GmailAuthError, load_credentials
    from .gmail.client import ApiGmailClient

    try:
        return ApiGmailClient(load_credentials(settings.gmail_token))
    except GmailAuthError:
        return None


def import_rejections(
    session: Session, settings: Settings, echo: Echo, client=None, *, recheck: bool = False
) -> None:
    from .gmail.client import body_fetcher
    from .linkedin.rejections import import_rejections as run

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


def import_applications(session: Session, settings: Settings, echo: Echo, client=None) -> None:
    from .applications import import_applications as run
    from .gmail.client import body_fetcher

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


def import_contacts(session: Session, echo: Echo) -> None:
    from .contacts import link_and_label

    stats = link_and_label(session)
    linked = stats.by_thread + stats.by_domain
    echo(
        f"contacts: {linked} linked ({stats.by_thread} by thread, {stats.by_domain} by domain), "
        f"{stats.probable} probable by name, {stats.labeled} labeled, "
        f"{stats.rejections} rejections from other mail"
    )


def run_sync(
    settings: Settings,
    echo: Echo,
    *,
    names: list[str] | None = None,
    since: date | None = None,
    progress_echo: Echo | None = None,
) -> None:
    """Gmail, then every import that builds on it. Raises GmailAuthError without a token."""
    from .gmail.auth import load_credentials
    from .gmail.client import ApiGmailClient
    from .gmail.sync import sync_gmail

    client = ApiGmailClient(load_credentials(settings.gmail_token))
    progress_echo = progress_echo or echo

    def progress(name: str, stats) -> None:
        if stats.threads % 50 == 0:
            progress_echo(f"{name}: {stats.threads} threads, {stats.new} new...")

    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            results = sync_gmail(session, client, names=names, since=since, progress=progress)
            import_linkedin(session, echo)
            if settings.linkedin_public_search:
                import_linkedin_search(session, settings, echo)
            if settings.telegram_mcp_command:
                import_telegram(session, settings, echo)
            import_rejections(session, settings, echo, client)
            import_applications(session, settings, echo, client)
            import_contacts(session, echo)
            # Last: the rules for fetched descriptions see applications/rejections above.
            if settings.linkedin_public_search:
                import_descriptions(session, settings, echo)
    finally:
        engine.dispose()
    for name, stats in results.items():
        echo(f"{name}: {stats.threads} threads, {stats.new} new, {stats.seen} already stored")
