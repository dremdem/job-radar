"""Shared pytest fixtures: a database copied from one migrated template per test, a
session, the web app client, and a temporary profile so tests never read the
developer's own.
"""

import collections.abc as cabc
import pathlib
import shutil

import fastapi.testclient as fastapi_testclient
import pytest
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.config as config
import job_radar.db as db
import job_radar.home as home
import job_radar.stack as stack
import job_radar.web.app as web_app

# Tests must not depend on the developer's local .env (e.g. opt-in flags turned on).
config.Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def profile_path(tmp_path, monkeypatch) -> pathlib.Path:
    """Where Home is read from in tests (the same file the web client's profile uses), never
    the developer's data/profile.yaml."""
    path = tmp_path / "profile.yaml"
    monkeypatch.setattr(home, "_profile_path", lambda: path)
    return path


@pytest.fixture
def thailand(profile_path: pathlib.Path) -> pathlib.Path:
    """A profile whose home is Thailand, in Asia / APAC."""
    stack.save_profile(profile_path, stack.Profile(home="Thailand", home_regions=["Asia", "APAC"]))
    return profile_path


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory) -> pathlib.Path:
    """Run the migrations once per test process; each test gets a copy of the file."""
    path = tmp_path_factory.mktemp("template") / "test.db"
    db.upgrade_database(f"sqlite:///{path}")
    return path


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(database_url: str, migrated_db: pathlib.Path, tmp_path) -> cabc.Iterator[sa.Engine]:
    shutil.copyfile(migrated_db, tmp_path / "test.db")
    engine = db.make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: sa.Engine) -> cabc.Iterator[sa_orm.Session]:
    with db.make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def client(
    engine: sa.Engine, database_url: str, tmp_path
) -> cabc.Iterator[fastapi_testclient.TestClient]:
    # Diagnostics go to a temp folder, never to the developer's data/.
    # ... and the profile too: tests must never touch the user's data/profile.yaml.
    settings = config.Settings(
        database_url=database_url,
        capture_debug_dir=tmp_path / "debug",
        profile=tmp_path / "profile.yaml",
    )
    app = web_app.create_app(settings, start_worker=False)
    with fastapi_testclient.TestClient(app) as client:
        yield client
