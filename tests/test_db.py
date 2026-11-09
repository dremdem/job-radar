"""SQLite connection settings (docs/architecture.md#database, #22)."""

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, text


def pragma(engine: Engine, name: str) -> str | int:
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar_one()


def test_connections_use_wal_busy_timeout_and_foreign_keys(engine: Engine):
    assert pragma(engine, "journal_mode") == "wal"
    assert pragma(engine, "busy_timeout") == 30000
    assert pragma(engine, "foreign_keys") == 1


def test_a_commit_does_not_wait_for_an_open_read(engine: Engine, tmp_path: Path):
    pragma(engine, "journal_mode")  # the engine has connected once: the file is in WAL now
    reader = sqlite3.connect(tmp_path / "test.db", isolation_level=None, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM companies").fetchone()  # holds a read snapshot
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=0"))  # fail at once instead of waiting
            conn.execute(
                text(
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
