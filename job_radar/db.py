from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _ensure_sqlite_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str) -> Engine:
    _ensure_sqlite_dir(database_url)
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    _ensure_sqlite_dir(database_url)
    command.upgrade(alembic_config(database_url), revision)
