"""The ``job-radar`` command line, built with Typer.

Commands: ``db upgrade`` and the duplicate merges, ``serve`` for the web UI, ``sync`` for
one source or all of them, ``import ...`` for single import steps over mail already in the
database, ``gmail auth`` for the one-time read-only consent, ``analyze`` for the local
model and ``demo`` for sample data. The commands are thin: the work is done in
``pipeline`` and the source modules. See docs/usage.md.

Usage:
    import job_radar.cli as cli
"""

import datetime as dt
import typing as t

import typer

import job_radar.config as config
import job_radar.db as db
import job_radar.demo as jr_demo
import job_radar.models as models
import job_radar.pipeline as pipeline

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
    settings = config.Settings()
    db.upgrade_database(settings.database_url, revision)
    typer.echo(f"Database is at {revision}: {settings.database_url}")


@db_app.command("merge-hh-duplicates")
def db_merge_hh_duplicates(
    apply: t.Annotated[bool, typer.Option("--apply", help="Merge; without it only list.")] = False,
) -> None:
    """Merge vacancies made from hh.ru notices into the same one from another source (#172)."""
    import job_radar.sources.hh.notifications as hh_notifications

    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            pairs = hh_notifications.merge_hh_duplicates(session, apply=apply)
    finally:
        engine.dispose()
    for keep, dup in pairs:
        typer.echo(f"#{dup} -> #{keep}")
    typer.echo(f"{len(pairs)} {'merged' if apply else 'to merge (dry run, add --apply)'}")


@db_app.command("merge-applied-duplicates")
def db_merge_applied_duplicates(
    apply: t.Annotated[bool, typer.Option("--apply", help="Merge; without it only list.")] = False,
    skip: t.Annotated[
        str, typer.Option(help="Applied vacancy ids to leave apart, comma-separated: 221,1628.")
    ] = "",
) -> None:
    """Merge copies of a vacancy you applied to into the applied one (#269)."""
    import job_radar.review.applied_duplicates as review_applied_duplicates

    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            left = frozenset(int(i) for i in skip.replace(" ", "").split(",") if i)
            plan = review_applied_duplicates.merge_applied_duplicates(
                session, apply=False, skip=left
            )
            for keep, copies in plan:
                o = session.get(models.Opportunity, keep)
                typer.echo(f"#{keep} {o.title} · {o.company.name}")
                for dup in copies:
                    d = session.get(models.Opportunity, dup)
                    typer.echo(f"    <- #{dup} {d.state.value} · {d.title}")
            if apply:
                review_applied_duplicates.merge_applied_duplicates(session, apply=True, skip=left)
    finally:
        engine.dispose()
    copies = sum(len(c) for _, c in plan)
    done = "merged" if apply else "to merge (dry run, add --apply)"
    typer.echo(f"{len(plan)} applied vacancies, {copies} copies {done}")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: t.Annotated[bool, typer.Option(help="Reload on code changes.")] = False,
) -> None:
    """Run the web UI."""
    import uvicorn

    uvicorn.run("job_radar.web.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def analyze() -> None:
    """Analyze descriptions in To review with the local model (docs/ai.md)."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            pipeline.analyze_descriptions(session, settings, typer.echo)
    finally:
        engine.dispose()


@app.command()
def demo(
    remove: t.Annotated[bool, typer.Option("--remove", help="Delete demo data instead.")] = False,
) -> None:
    """Add a few demo opportunities to try the review queue."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            if remove:
                typer.echo(f"Removed {jr_demo.remove_demo(session)} demo opportunities.")
            else:
                typer.echo(f"Added {jr_demo.seed_demo(session)} demo opportunities.")
    finally:
        engine.dispose()


@gmail_app.command("auth")
def gmail_auth() -> None:
    """Open the browser for one-time read-only Gmail consent and store the token."""
    import job_radar.sources.gmail.auth as jr_gmail_auth

    settings = config.Settings()
    try:
        jr_gmail_auth.authorize(settings.gmail_client_secret, settings.gmail_token)
    except jr_gmail_auth.GmailAuthError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Token saved to {settings.gmail_token}")


@import_app.command("linkedin")
def import_linkedin() -> None:
    """Create opportunities from LinkedIn alert emails already in the database."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            _import_linkedin(session)
    finally:
        engine.dispose()


@import_app.command("linkedin-search")
def import_linkedin_search() -> None:
    """Opt-in: first page of LinkedIn's public search for each new alert (no login)."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            _import_linkedin_search(session, settings)
    finally:
        engine.dispose()


@import_app.command("descriptions")
def import_descriptions() -> None:
    """Opt-in: descriptions for To review from LinkedIn's public job pages (no login)."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            pipeline.import_descriptions(session, settings, typer.echo)
    finally:
        engine.dispose()


@import_app.command("rejections")
def import_rejections(
    recheck: t.Annotated[
        bool, typer.Option("--recheck", help="Re-examine emails earlier runs found not rejections.")
    ] = False,
) -> None:
    """Record rejections from LinkedIn application-update emails already in the database."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            _import_rejections(session, recheck=recheck)
    finally:
        engine.dispose()


@import_app.command("applications")
def import_applications() -> None:
    """Past applications from confirmation emails already in the database."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            _import_applications(session)
    finally:
        engine.dispose()


@import_app.command("contacts")
def import_contacts() -> None:
    """Link mail to companies and label it, for company history in the queue."""
    settings = config.Settings()
    db.upgrade_database(settings.database_url)
    engine = db.make_engine(settings.database_url)
    try:
        with db.make_session_factory(engine)() as session:
            _import_contacts(session)
    finally:
        engine.dispose()


@app.command()
def sync(
    source: models.SourceType,
    query: t.Annotated[
        list[str] | None, typer.Option("--query", "-q", help="Gmail query name; repeatable.")
    ] = None,
    since: t.Annotated[
        dt.datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Override the cursor.")
    ] = None,
) -> None:
    """Import new items from a source."""
    steps = {
        models.SourceType.TELEGRAM: pipeline.import_telegram,
        models.SourceType.HIMALAYAS: pipeline.import_himalayas,
        models.SourceType.HACKERNEWS: pipeline.import_hackernews,
        models.SourceType.REMOTEOK: pipeline.import_remoteok,
        models.SourceType.JOBICY: pipeline.import_jobicy,
        models.SourceType.FDETIMES: pipeline.import_fdetimes,
        # One step reads all three small boards (docs/boards.md).
        models.SourceType.REMOTIVE: pipeline.import_boards,
        models.SourceType.WORKINGNOMADS: pipeline.import_boards,
        models.SourceType.WEWORKREMOTELY: pipeline.import_boards,
    }
    if source in steps:
        step = steps[source]
        settings = config.Settings()
        db.upgrade_database(settings.database_url)
        engine = db.make_engine(settings.database_url)
        try:
            with db.make_session_factory(engine)() as session:
                step(session, settings, typer.echo)
        finally:
            engine.dispose()
        return
    if source is not models.SourceType.GMAIL:
        typer.echo(f"sync {source.value}: not implemented yet", err=True)
        raise typer.Exit(code=1)
    _sync_gmail(query, since)


# Thin wrappers: the pipeline lives in job_radar/pipeline.py, shared with the web UI.
def _import_linkedin(session) -> None:
    pipeline.import_linkedin(session, typer.echo)


def _import_linkedin_search(session, settings: config.Settings) -> None:
    pipeline.import_linkedin_search(session, settings, typer.echo)


def _import_rejections(session, client=None, *, recheck: bool = False) -> None:
    pipeline.import_rejections(session, config.Settings(), typer.echo, client, recheck=recheck)


def _import_applications(session, client=None) -> None:
    pipeline.import_applications(session, config.Settings(), typer.echo, client)


def _import_contacts(session) -> None:
    pipeline.import_contacts(session, typer.echo)


def _sync_gmail(names: list[str] | None, since: dt.datetime | None) -> None:
    import job_radar.sources.gmail.queries as gmail_queries

    unknown = set(names or []) - gmail_queries.QUERIES.keys()
    if unknown:
        raise typer.BadParameter(
            f"Unknown query {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(gmail_queries.QUERIES)}",
            param_hint="--query",
        )
    steps = pipeline.run_sync(
        config.Settings(),
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
