from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from job_radar.db import upgrade_database
from job_radar.models import Base

EXPECTED_TABLES = {
    "companies",
    "opportunities",
    "source_records",
    "job_snapshots",
    "applications",
    "email_messages",
    "sync_cursors",
    "rejections",
}


def test_upgrade_creates_all_tables(engine):
    tables = set(inspect(engine).get_table_names())
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent(engine, database_url):
    upgrade_database(database_url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
