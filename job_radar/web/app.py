"""The web application: the review queue and every page around it.

``create_app`` builds the FastAPI app with its database session, background jobs and
Claude workers. Pages: the queue (``/``, ``/opportunities``) with a vacancy panel, notes,
history and chat; Applications, Skills, Memory, Profile and Settings; the LinkedIn request
log. Buttons post to small endpoints (``/sync``, ``/descriptions``, ``/chrome-fill``,
``/claude/analyze``) that start background work and report its status for htmx to poll.
``/api/capture`` receives pages from the bookmarklet. ``/favicon.svg`` is the tab
icon (``favicon.svg`` next to this module). See docs/queue.md and
docs/settings.md.

Usage:
    import job_radar.web.app as web_app
"""

import collections.abc as cabc
import contextlib
import pathlib

import fastapi
import fastapi.responses as fastapi_responses

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.runner as jr_claude_runner
import job_radar.config as config
import job_radar.db as db
import job_radar.jobs.description_job as jobs_description_job
import job_radar.jobs.pipeline as jobs_pipeline
import job_radar.jobs.sync_job as jobs_sync_job
import job_radar.web.routes.applications as routes_applications
import job_radar.web.routes.capture as routes_capture
import job_radar.web.routes.claude as routes_claude
import job_radar.web.routes.jobs as routes_jobs
import job_radar.web.routes.queue as routes_queue
import job_radar.web.routes.settings as routes_settings
import job_radar.web.templating as web_templating

# The tab icon (#70): a radar with a sweep and a blip, in the inbox colours.
FAVICON = (pathlib.Path(__file__).parent / "favicon.svg").read_text()


def create_app(
    settings: config.Settings | None = None,
    sync_runner: jobs_sync_job.Runner | None = None,
    claude_runner: jr_claude_runner.Runner | None = None,
    start_worker: bool = True,
    description_runner: cabc.Callable[[jobs_description_job.DescriptionJob], None] | None = None,
    chrome_runner: cabc.Callable[[jobs_description_job.DescriptionJob], None] | None = None,
) -> fastapi.FastAPI:
    """``start_worker=False``: tests drive the Claude worker with ``run_once``."""
    settings = settings or config.get_settings()
    sync_job = jobs_sync_job.SyncJob()
    description_job = jobs_description_job.DescriptionJob()
    description_runner = description_runner or (
        lambda job: jobs_pipeline.description_job(settings, job)
    )

    def start_descriptions() -> bool:
        # one LinkedIn description run at a time
        if chrome_job.state == "running":
            return False
        return description_job.start(description_runner)

    # The Chrome fill (#247): descriptions read in your own Chrome; tests pass a fake.
    chrome_job = jobs_description_job.DescriptionJob(blocked_note="stopped: {reason}")
    chrome_runner = chrome_runner or (lambda job: jobs_pipeline.chrome_fill_job(settings, job))

    # The Sync button runs the same pipeline as `./jr sync`, but leaves the slow LinkedIn
    # descriptions to the background job (#205); tests pass a fake.
    sync_runner = sync_runner or (
        lambda echo, on_steps: jobs_pipeline.run_sync(
            settings, echo, on_steps=on_steps, start_descriptions=start_descriptions
        )
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: fastapi.FastAPI) -> cabc.AsyncIterator[None]:
        # New code may bring new tables; the server must not wait for a CLI command.
        db.upgrade_database(settings.database_url)
        engine = db.make_engine(settings.database_url)
        app.state.session_factory = db.make_session_factory(engine)
        app.state.settings = settings
        worker = claude_analysis.Worker(
            app.state.session_factory,
            claude_runner
            or jr_claude_runner.ClaudeCli(settings.claude_command, settings.claude_workdir),
            profile_path=settings.profile,
            cv_path=settings.cv_path,
        )
        app.state.claude_worker = worker
        # The status line says when a usage-limit pause ends (#197).
        web_templating.templates.env.globals["claude_paused_until"] = lambda: (
            web_templating.pause_left(worker.paused_until)
        )
        app.state.claude_worker_on = start_worker and settings.claude_worker
        if app.state.claude_worker_on:
            worker.start()
        yield
        worker.stop()
        engine.dispose()

    app = fastapi.FastAPI(title="Job Radar", lifespan=lifespan)
    # What the routes read through job_radar.web.deps.
    app.state.settings = settings
    app.state.sync_job = sync_job
    app.state.sync_runner = sync_runner
    app.state.description_job = description_job
    app.state.chrome_job = chrome_job
    app.state.chrome_runner = chrome_runner
    app.state.start_descriptions = start_descriptions

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Browsers ask for /favicon.ico on their own; both paths give the SVG.
    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> fastapi_responses.Response:
        return fastapi_responses.Response(FAVICON, media_type="image/svg+xml")

    app.include_router(routes_queue.router)
    app.include_router(routes_claude.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_capture.router)
    app.include_router(routes_applications.router)
    return app
