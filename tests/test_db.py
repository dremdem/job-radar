"""SQLite connection settings (docs/architecture.md#database, #22)."""

import pathlib
import sqlite3

import sqlalchemy as sa


def pragma(engine: sa.Engine, name: str) -> str | int:
    with engine.connect() as conn:
        return conn.execute(sa.text(f"PRAGMA {name}")).scalar_one()


def test_connections_use_wal_busy_timeout_and_foreign_keys(engine: sa.Engine):
    assert pragma(engine, "journal_mode") == "wal"
    assert pragma(engine, "busy_timeout") == 30000
    assert pragma(engine, "foreign_keys") == 1


def test_a_commit_does_not_wait_for_an_open_read(engine: sa.Engine, tmp_path: pathlib.Path):
    # the engine has connected once: the file is in WAL now
    pragma(engine, "journal_mode")
    reader = sqlite3.connect(tmp_path / "test.db", isolation_level=None, timeout=0)
    reader.execute("BEGIN")
    # holds a read snapshot
    reader.execute("SELECT count(*) FROM companies").fetchone()
    try:
        with engine.begin() as conn:
            # fail at once instead of waiting
            conn.execute(sa.text("PRAGMA busy_timeout=0"))
            conn.execute(
                sa.text(
                    "INSERT INTO companies (name, normalized_name, aliases, domains, created_at) "
                    "VALUES ('Acme', 'acme', '[]', '[]', CURRENT_TIMESTAMP)"
                )
            )
        # The reader still sees its snapshot; a new read sees the commit.
        assert reader.execute("SELECT count(*) FROM companies").fetchone() == (0,)
        reader.execute("COMMIT")
        assert reader.execute("SELECT count(*) FROM companies").fetchone() == (1,)
    finally:
        reader.close()
