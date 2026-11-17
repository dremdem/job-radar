"""Tests for job_radar.scoring.description: Python mentions in a description, their quote,
score effect and highlighting (docs/queue.md#description-checks).
"""

import json

import paths

import job_radar.scoring.description as scoring_description

EMPAT = json.loads((paths.FIXTURES / "linkedin_capture" / "semantic_search.json").read_text())[
    "detail"
]["description"]
ECARSTRADE = (
    "Strong Python 3.10+ with production experience in async programming (asyncio) and FastAPI "
    "(or similar). Solid experience with message queues, ideally RabbitMQ (aio_pika)."
)


def test_real_description_mentions_python():
    check = scoring_description.check_python(EMPAT)
    assert check.mentions >= 1
    assert "python" in check.quote.lower()


def test_counts_python_forms_and_related_tools():
    check = scoring_description.check_python(
        ECARSTRADE + " We like python-based tools and Python3 scripts."
    )
    assert check.mentions == 3
    assert check.related == ["asyncio", "FastAPI"]
    assert check.quote.startswith("Strong Python 3.10+")


def test_no_python():
    check = scoring_description.check_python("We build services in Go and Rust on Kubernetes.")
    assert (check.mentions, check.related, check.quote) == (0, [], "")


def test_quote_is_short_and_around_the_mention():
    text = "x " * 400 + "Experience with Python is required." + " y" * 400
    quote = scoring_description.check_python(text).quote
    assert "Python is required" in quote and len(quote) <= 220


def test_python_risk():
    points, reason = scoring_description.python_risk("Backend Engineer", ECARSTRADE)
    assert points > 0 and reason.sign == "+"
    assert reason.text == "Python in description (x1; also asyncio, FastAPI)"
    points, reason = scoring_description.python_risk("Backend Engineer", "Go and Rust only.")
    assert (points, reason.sign, reason.text) == (-3.0, "-", "No Python in description")


def test_python_risk_needs_a_description_and_respects_the_title():
    assert scoring_description.python_risk("Backend Engineer", None) is None
    assert scoring_description.python_risk("Senior Python Developer", "Go and Rust only.") is None


def test_latest_snapshot_mixes_stored_and_new_snapshots(session):
    """A snapshot from SQLite is naive, one just added is aware: comparing them used to fail."""

    import job_radar.dedup as dedup
    import job_radar.models as models

    o = models.Opportunity(
        title="Python Developer", company=dedup.get_or_create_company(session, "Acme")
    )
    session.add(o)
    session.flush()
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_CAPTURE,
        external_id="1",
        url=None,
        opportunity=o,
    )
    dedup.add_snapshot(session, record, "old text")
    session.commit()
    # the old snapshot now comes back from SQLite, without tzinfo
    session.expire_all()
    record = o.source_records[0]
    _, created = dedup.add_snapshot(session, record, "new text")
    assert created and record.snapshots[-1].retrieved_at.tzinfo is not None
    assert scoring_description.latest_description(o) == "new text"
