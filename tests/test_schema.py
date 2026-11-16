"""Tests for the database schema: migrations create every table, match the models and
can run twice (docs/data-model.md).
"""

import alembic.autogenerate as alembic_autogenerate
import alembic.migration as alembic_migration
import sqlalchemy as sa

import job_radar.db as db
import job_radar.models as models

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
    tables = set(sa.inspect(engine).get_table_names())
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        diff = alembic_autogenerate.compare_metadata(
            alembic_migration.MigrationContext.configure(conn), models.Base.metadata
        )
    assert diff == []


def test_upgrade_is_idempotent(engine, database_url):
    db.upgrade_database(database_url)
    assert set(sa.inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_migration_turns_unavailable_events_into_the_flag(database_url):
    db.upgrade_database(database_url, "8c3f5b2d9e61")
    engine = sa.create_engine(database_url)
    t = {"now": "2026-11-08 07:08:12"}
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO companies (id, name, normalized_name, aliases, domains, created_at)"
                " VALUES (1, 'Acme', 'acme', '[]', '[]', :now)"
            ),
            t,
        )
        for oid, state in ((1, "new"), (2, "ignore"), (3, "new")):
            c.execute(
                sa.text(
                    "INSERT INTO opportunities"
                    " (id, title, company_id, state, score_reasons, created_at, updated_at)"
                    " VALUES (:oid, 'T', 1, :state, '[]', :now, :now)"
                ),
                t | {"oid": oid, "state": state},
            )
        for oid, kind in ((1, "unavailable"), (2, "unavailable"), (3, "fill_problem")):
            c.execute(
                sa.text(
                    "INSERT INTO company_events (company_id, opportunity_id, kind, at, created_at)"
                    " VALUES (1, :oid, :kind, :now, :now)"
                ),
                t | {"oid": oid, "kind": kind},
            )
    db.upgrade_database(database_url)
    with engine.connect() as c:
        rows = c.execute(sa.text("SELECT id, state, auto_ignore_reason, unavailable_at IS NOT NULL"
                                 " FROM opportunities ORDER BY id")).all()  # fmt: skip
        kinds = c.execute(sa.text("SELECT kind FROM company_events")).scalars().all()
    assert [tuple(r) for r in rows] == [
        (1, "auto_ignore", "vacancy not available", 1),
        (2, "ignore", None, 1),
        (3, "new", None, 0),
    ]
    assert kinds == ["fill_problem"]
