"""Database engine, sessions and migrations.

``make_engine`` creates the SQLAlchemy engine; for SQLite it creates the data folder and
turns on WAL, a busy timeout and foreign keys, because background jobs and web requests
share the file. ``make_session_factory`` gives sessions that keep objects usable after
commit. ``upgrade_database`` applies the Alembic migrations from
``job_radar/migrations``. See docs/architecture.md#database.

Usage:
    import job_radar.db as db
"""

import pathlib

import alembic.command as alembic_command
import alembic.config as alembic_config_mod
import sqlalchemy as sa
import sqlalchemy.engine as sa_engine
import sqlalchemy.event as sa_event
import sqlalchemy.orm as sa_orm

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"
BUSY_TIMEOUT_MS = 30_000


def _ensure_sqlite_dir(database_url: str) -> None:
    url = sa_engine.make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        pathlib.Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str) -> sa.Engine:
    _ensure_sqlite_dir(database_url)
    engine = sa.create_engine(database_url)
    if engine.dialect.name == "sqlite":

        @sa_event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _record) -> None:
            # docs/architecture.md#database: background threads and web requests share the
            # file; WAL lets reads and writes overlap, busy_timeout makes a writer wait.
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session_factory(engine: sa.Engine) -> sa_orm.sessionmaker[sa_orm.Session]:
    return sa_orm.sessionmaker(engine, expire_on_commit=False)


def alembic_config(database_url: str) -> alembic_config_mod.Config:
    config = alembic_config_mod.Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    _ensure_sqlite_dir(database_url)
    alembic_command.upgrade(alembic_config(database_url), revision)
