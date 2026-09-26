from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from job_radar.config import Settings
from job_radar.db import make_engine, make_session_factory, upgrade_database
from job_radar.web.app import create_app

# Tests must not depend on the developer's local .env (e.g. opt-in flags turned on).
Settings.model_config["env_file"] = None


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    upgrade_database(database_url)
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def client(engine: Engine, database_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as client:
        yield client
