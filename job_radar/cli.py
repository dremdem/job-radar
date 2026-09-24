from datetime import datetime
from typing import Annotated

import typer

from .config import Settings
from .db import make_engine, make_session_factory, upgrade_database
from .demo import remove_demo, seed_demo
from .models import SourceType

app = typer.Typer(help="Job Radar: local-first job opportunity review.", no_args_is_help=True)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")
gmail_app = typer.Typer(help="Gmail (read-only).", no_args_is_help=True)
app.add_typer(gmail_app, name="gmail")
import_app = typer.Typer(help="Turn stored data into opportunities.", no_args_is_help=True)
app.add_typer(import_app, name="import")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations to the configured database."""
    settings = Settings()
    upgrade_database(settings.database_url, revision)
    typer.echo(f"Database is at {revision}: {settings.database_url}")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on code changes.")] = False,
) -> None:
    """Run the web UI."""
    import uvicorn

    uvicorn.run("job_radar.web.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def demo(
    remove: Annotated[bool, typer.Option("--remove", help="Delete demo data instead.")] = False,
) -> None:
    """Add a few demo opportunities to try the review queue."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            if remove:
                typer.echo(f"Removed {remove_demo(session)} demo opportunities.")
            else:
                typer.echo(f"Added {seed_demo(session)} demo opportunities.")
    finally:
        engine.dispose()


@gmail_app.command("auth")
def gmail_auth() -> None:
    """Open the browser for one-time read-only Gmail consent and store the token."""
    from .gmail.auth import GmailAuthError, authorize

    settings = Settings()
    try:
        authorize(settings.gmail_client_secret, settings.gmail_token)
    except GmailAuthError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Token saved to {settings.gmail_token}")


@import_app.command("linkedin")
def import_linkedin() -> None:
    """Create opportunities from LinkedIn alert emails already in the database."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            _import_linkedin(session)
    finally:
        engine.dispose()


def _import_linkedin(session) -> None:
    from .linkedin.alerts import import_alerts

    stats = import_alerts(session)
    typer.echo(f"linkedin: {stats.emails} alert emails, {stats.jobs} jobs, {stats.new} new")


@app.command()
def sync(
    source: SourceType,
    query: Annotated[
        list[str] | None, typer.Option("--query", "-q", help="Gmail query name; repeatable.")
    ] = None,
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Override the cursor.")
    ] = None,
) -> None:
    """Import new items from a source."""
    if source is not SourceType.GMAIL:
        typer.echo(f"sync {source.value}: not implemented yet", err=True)
        raise typer.Exit(code=1)
    _sync_gmail(query, since)


def _report_progress(name: str, stats) -> None:
    if stats.threads % 50 == 0:
        typer.echo(f"{name}: {stats.threads} threads, {stats.new} new...", err=True)


def _sync_gmail(names: list[str] | None, since: datetime | None) -> None:
    from .gmail.auth import GmailAuthError, load_credentials
    from .gmail.client import ApiGmailClient
    from .gmail.queries import QUERIES
    from .gmail.sync import sync_gmail

    unknown = set(names or []) - QUERIES.keys()
    if unknown:
        raise typer.BadParameter(
            f"Unknown query {', '.join(sorted(unknown))}. Known: {', '.join(QUERIES)}",
            param_hint="--query",
        )
    settings = Settings()
    try:
        client = ApiGmailClient(load_credentials(settings.gmail_token))
    except GmailAuthError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            results = sync_gmail(
                session,
                client,
                names=names,
                since=since.date() if since else None,
                progress=_report_progress,
            )
            _import_linkedin(session)
    finally:
        engine.dispose()
    for name, stats in results.items():
        typer.echo(f"{name}: {stats.threads} threads, {stats.new} new, {stats.seen} already stored")
