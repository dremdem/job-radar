import json
from pathlib import Path

from job_radar.description import check_python, python_risk

EMPAT = json.loads(
    (Path(__file__).parent / "fixtures" / "linkedin_capture" / "semantic_search.json").read_text()
)["detail"]["description"]
ECARSTRADE = (
    "Strong Python 3.10+ with production experience in async programming (asyncio) and FastAPI "
    "(or similar). Solid experience with message queues, ideally RabbitMQ (aio_pika)."
)


def test_real_description_mentions_python():
    check = check_python(EMPAT)
    assert check.mentions >= 1
    assert "python" in check.quote.lower()


def test_counts_python_forms_and_related_tools():
    check = check_python(ECARSTRADE + " We like python-based tools and Python3 scripts.")
    assert check.mentions == 3
    assert check.related == ["asyncio", "FastAPI"]
    assert check.quote.startswith("Strong Python 3.10+")


def test_no_python():
    check = check_python("We build services in Go and Rust on Kubernetes.")
    assert (check.mentions, check.related, check.quote) == (0, [], "")


def test_quote_is_short_and_around_the_mention():
    text = "x " * 400 + "Experience with Python is required." + " y" * 400
    quote = check_python(text).quote
    assert "Python is required" in quote and len(quote) <= 220


def test_python_risk():
    points, reason = python_risk("Backend Engineer", ECARSTRADE)
    assert points > 0 and reason.sign == "+"
    assert reason.text == "Python in description (x1; also asyncio, FastAPI)"
    points, reason = python_risk("Backend Engineer", "Go and Rust only.")
    assert (points, reason.sign, reason.text) == (-3.0, "-", "No Python in description")


def test_python_risk_needs_a_description_and_respects_the_title():
    assert python_risk("Backend Engineer", None) is None
    assert python_risk("Senior Python Developer", "Go and Rust only.") is None


def test_latest_snapshot_mixes_stored_and_new_snapshots(session):
    """A snapshot from SQLite is naive, one just added is aware: comparing them used to fail."""

    from job_radar.dedup import add_snapshot, get_or_create_company, upsert_source_record
    from job_radar.description import latest_description
    from job_radar.models import Opportunity, SourceType

    o = Opportunity(title="Python Developer", company=get_or_create_company(session, "Acme"))
    session.add(o)
    session.flush()
    record = upsert_source_record(
        session, source_type=SourceType.LINKEDIN_CAPTURE, external_id="1", url=None, opportunity=o
    )
    add_snapshot(session, record, "old text")
    session.commit()
    session.expire_all()  # the old snapshot now comes back from SQLite, without tzinfo
    record = o.source_records[0]
    _, created = add_snapshot(session, record, "new text")
    assert created and record.snapshots[-1].retrieved_at.tzinfo is not None
    assert latest_description(o) == "new text"
