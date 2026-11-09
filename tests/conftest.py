import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from job_radar import home as home_module
from job_radar.config import Settings
from job_radar.db import make_engine, make_session_factory, upgrade_database
from job_radar.stack import Profile, save_profile
from job_radar.web.app import create_app

# Tests must not depend on the developer's local .env (e.g. opt-in flags turned on).
Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def profile_path(tmp_path, monkeypatch) -> Path:
    """Where Home is read from in tests (the same file the web client's profile uses), never
    the developer's data/profile.yaml."""
    path = tmp_path / "profile.yaml"
    monkeypatch.setattr(home_module, "_profile_path", lambda: path)
    return path


@pytest.fixture
def thailand(profile_path: Path) -> Path:
    """A profile whose home is Thailand, in Asia / APAC."""
    save_profile(profile_path, Profile(home="Thailand", home_regions=["Asia", "APAC"]))
    return profile_path


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory) -> Path:
    """Run the migrations once per test process; each test gets a copy of the file."""
    path = tmp_path_factory.mktemp("template") / "test.db"
    upgrade_database(f"sqlite:///{path}")
    return path


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(database_url: str, migrated_db: Path, tmp_path) -> Iterator[Engine]:
    shutil.copyfile(migrated_db, tmp_path / "test.db")
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def client(engine: Engine, database_url: str, tmp_path) -> Iterator[TestClient]:
    # Diagnostics go to a temp folder, never to the developer's data/.
    # ... and the profile too: tests must never touch the user's data/profile.yaml.
    settings = Settings(
        database_url=database_url,
        capture_debug_dir=tmp_path / "debug",
        profile=tmp_path / "profile.yaml",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        yield client
