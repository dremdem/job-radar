from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BUSY_TIMEOUT_MS = 30_000


def _ensure_sqlite_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str) -> Engine:
    _ensure_sqlite_dir(database_url)
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _record) -> None:
            # docs/architecture.md#database: background threads and web requests share the
            # file; WAL lets reads and writes overlap, busy_timeout makes a writer wait.
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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
