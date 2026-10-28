from datetime import datetime
from typing import Annotated

import typer

from . import pipeline
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


@db_app.command("merge-hh-duplicates")
def db_merge_hh_duplicates(
    apply: Annotated[bool, typer.Option("--apply", help="Merge; without it only list.")] = False,
) -> None:
    """Merge vacancies made from hh.ru notices into the same one from another source (#172)."""
    from .hh.notifications import merge_hh_duplicates

    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            pairs = merge_hh_duplicates(session, apply=apply)
    finally:
        engine.dispose()
    for keep, dup in pairs:
        typer.echo(f"#{dup} -> #{keep}")
    typer.echo(f"{len(pairs)} {'merged' if apply else 'to merge (dry run, add --apply)'}")


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
def analyze() -> None:
    """Analyze descriptions in To review with the local model (docs/ai.md)."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            pipeline.analyze_descriptions(session, settings, typer.echo)
    finally:
        engine.dispose()


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


@import_app.command("linkedin-search")
def import_linkedin_search() -> None:
    """Opt-in: first page of LinkedIn's public search for each new alert (no login)."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            _import_linkedin_search(session, settings)
    finally:
        engine.dispose()


@import_app.command("descriptions")
def import_descriptions() -> None:
    """Opt-in: descriptions for To review from LinkedIn's public job pages (no login)."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            pipeline.import_descriptions(session, settings, typer.echo)
    finally:
        engine.dispose()


@import_app.command("rejections")
def import_rejections(
    recheck: Annotated[
        bool, typer.Option("--recheck", help="Re-examine emails earlier runs found not rejections.")
    ] = False,
) -> None:
    """Record rejections from LinkedIn application-update emails already in the database."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            _import_rejections(session, recheck=recheck)
    finally:
        engine.dispose()


@import_app.command("applications")
def import_applications() -> None:
    """Past applications from confirmation emails already in the database."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            _import_applications(session)
    finally:
        engine.dispose()


@import_app.command("contacts")
def import_contacts() -> None:
    """Link mail to companies and label it, for company history in the queue."""
    settings = Settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            _import_contacts(session)
    finally:
        engine.dispose()


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
    steps = {
        SourceType.TELEGRAM: pipeline.import_telegram,
        SourceType.HIMALAYAS: pipeline.import_himalayas,
        SourceType.HACKERNEWS: pipeline.import_hackernews,
        SourceType.REMOTEOK: pipeline.import_remoteok,
        SourceType.JOBICY: pipeline.import_jobicy,
    }
    if source in steps:
        step = steps[source]
        settings = Settings()
        upgrade_database(settings.database_url)
        engine = make_engine(settings.database_url)
        try:
            with make_session_factory(engine)() as session:
                step(session, settings, typer.echo)
        finally:
            engine.dispose()
        return
    if source is not SourceType.GMAIL:
        typer.echo(f"sync {source.value}: not implemented yet", err=True)
        raise typer.Exit(code=1)
    _sync_gmail(query, since)


# Thin wrappers: the pipeline lives in job_radar/pipeline.py, shared with the web UI.
def _import_linkedin(session) -> None:
    pipeline.import_linkedin(session, typer.echo)


def _import_linkedin_search(session, settings: Settings) -> None:
    pipeline.import_linkedin_search(session, settings, typer.echo)


def _import_rejections(session, client=None, *, recheck: bool = False) -> None:
    pipeline.import_rejections(session, Settings(), typer.echo, client, recheck=recheck)


def _import_applications(session, client=None) -> None:
    pipeline.import_applications(session, Settings(), typer.echo, client)


def _import_contacts(session) -> None:
    pipeline.import_contacts(session, typer.echo)


def _sync_gmail(names: list[str] | None, since: datetime | None) -> None:
    from .gmail.queries import QUERIES

    unknown = set(names or []) - QUERIES.keys()
    if unknown:
        raise typer.BadParameter(
            f"Unknown query {', '.join(sorted(unknown))}. Known: {', '.join(QUERIES)}",
            param_hint="--query",
        )
    steps = pipeline.run_sync(
        Settings(),
        typer.echo,
        names=names,
        since=since.date() if since else None,
        progress_echo=lambda line: typer.echo(line, err=True),
    )
    failed = [s.key for s in steps if s.state == "failed"]
    if failed:
        word = "step" if len(failed) == 1 else "steps"
        typer.echo(f"{len(failed)} {word} failed: {', '.join(failed)}", err=True)
        raise typer.Exit(code=1)
