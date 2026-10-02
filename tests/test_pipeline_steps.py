from google.auth.exceptions import RefreshError

from job_radar import pipeline
from job_radar.config import Settings


def settings(tmp_path, database_url, **extra) -> Settings:
    base = {
        "database_url": database_url,
        "gmail_token": tmp_path / "missing.json",
        "himalayas": False,
        "ai_analysis": False,
    }
    return Settings(**(base | extra))


def by_key(steps):
    return {s.key: s for s in steps}


def test_gmail_failure_does_not_stop_the_other_steps(tmp_path, engine, database_url):
    lines, snapshots = [], []
    steps = pipeline.run_sync(
        settings(tmp_path, database_url),
        lines.append,
        on_steps=lambda s: snapshots.append([(x.key, x.state) for x in s]),
    )
    found = by_key(steps)
    assert found["gmail"].state == "failed"
    assert "./jr auth" in found["gmail"].summary
    for key in ("linkedin", "mail"):
        assert found[key].state == "done", found[key]
    # Disabled sources are listed as off, not run.
    for key in ("linkedin_search", "telegram", "himalayas", "descriptions", "ai"):
        assert found[key].state == "off"
    # Progress is reported while it runs: the first snapshot has everything waiting.
    assert all(state in ("waiting", "off") for _, state in snapshots[0])
    assert ("gmail", "running") in snapshots[1]
    assert any("rejections:" in line for line in lines)


def test_expired_token_reads_as_a_hint():
    error = RefreshError("invalid_grant: Token has been expired or revoked.", {})
    assert pipeline.describe_error(error) == (
        "Gmail access expired or revoked: run `./jr auth` (docs/gmail.md)"
    )


def test_a_failing_source_is_reported_and_the_rest_runs(
    tmp_path, engine, database_url, monkeypatch
):
    def broken(session, settings, echo, client=None):
        raise RuntimeError("Himalayas is down")

    monkeypatch.setattr(pipeline, "import_himalayas", broken)
    steps = by_key(pipeline.run_sync(settings(tmp_path, database_url, himalayas=True), print))
    assert steps["himalayas"].state == "failed"
    assert steps["himalayas"].summary == "Himalayas is down"
    assert steps["mail"].state == "done"


def test_summary_is_the_step_result_without_prefix(tmp_path, engine, database_url, monkeypatch):
    def fake(session, settings, echo, client=None):
        echo("himalayas: 5 pages, 75 fresh jobs, 42 new")

    monkeypatch.setattr(pipeline, "import_himalayas", fake)
    steps = by_key(pipeline.run_sync(settings(tmp_path, database_url, himalayas=True), print))
    assert steps["himalayas"].summary == "5 pages, 75 fresh jobs, 42 new"
