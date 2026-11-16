"""FastAPI dependencies of the web UI: the database session and what lives on app.state.

Usage:
    import job_radar.web.deps as web_deps
"""

import collections.abc as cabc
import typing as t

import fastapi
import sqlalchemy.orm as sa_orm

import job_radar.config as config
import job_radar.jobs.description_job as jobs_description_job
import job_radar.jobs.sync_job as jobs_sync_job


def get_session(request: fastapi.Request) -> cabc.Iterator[sa_orm.Session]:
    with request.app.state.session_factory() as session:
        # Row helpers below read per-user files (the profile) through the session.
        session.info["settings"] = request.app.state.settings
        yield session


SessionDep = t.Annotated[sa_orm.Session, fastapi.Depends(get_session)]


def _from_state(name: str) -> cabc.Callable[[fastapi.Request], t.Any]:
    """A dependency that reads one attribute of ``app.state`` (set in ``create_app``)."""

    def read(request: fastapi.Request) -> t.Any:
        return getattr(request.app.state, name)

    return read


SettingsDep = t.Annotated[config.Settings, fastapi.Depends(_from_state("settings"))]
SyncJobDep = t.Annotated[jobs_sync_job.SyncJob, fastapi.Depends(_from_state("sync_job"))]
SyncRunnerDep = t.Annotated[jobs_sync_job.Runner, fastapi.Depends(_from_state("sync_runner"))]
DescriptionJobDep = t.Annotated[
    jobs_description_job.DescriptionJob, fastapi.Depends(_from_state("description_job"))
]
ChromeJobDep = t.Annotated[
    jobs_description_job.DescriptionJob, fastapi.Depends(_from_state("chrome_job"))
]
ChromeRunnerDep = t.Annotated[
    cabc.Callable[[jobs_description_job.DescriptionJob], None],
    fastapi.Depends(_from_state("chrome_runner")),
]
StartDescriptionsDep = t.Annotated[
    cabc.Callable[[], bool], fastapi.Depends(_from_state("start_descriptions"))
]
