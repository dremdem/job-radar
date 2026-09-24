from datetime import UTC, datetime

from gmail_fakes import b64, message

from job_radar.gmail.parse import parse_message
from job_radar.models import EmailDirection

SENT_AT = datetime(2026, 9, 1, 10, 30, tzinfo=UTC)


def test_parse_headers_and_date():
    parsed = parse_message(message("m1", "t1", sent_at=SENT_AT, subject="Hi"))
    assert parsed.gmail_message_id == "m1"
    assert parsed.gmail_thread_id == "t1"
    assert parsed.sent_at == SENT_AT
    assert parsed.from_addr == "Recruiter <jane@acme.example>"
    assert parsed.to_addr == "me@example.com"
    assert parsed.subject == "Hi"
    assert parsed.snippet == "Hello there"
    assert parsed.direction is EmailDirection.IN


def test_sent_label_means_outgoing():
    parsed = parse_message(message("m1", "t1", sent_at=SENT_AT, labels=("SENT",)))
    assert parsed.direction is EmailDirection.OUT


def test_extracts_html_and_text_bodies():
    parsed = parse_message(message("m1", "t1", sent_at=SENT_AT, html="<p>Ол</p>", text="Ол"))
    assert parsed.body_html == "<p>Ол</p>"
    assert parsed.body_text == "Ол"


def test_nested_parts_and_single_part_body():
    raw = message("m1", "t1", sent_at=SENT_AT, html=None, text=None)
    raw["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html", "body": {"data": b64("<i>deep</i>")}}],
        },
        {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "x"}},
    ]
    assert parse_message(raw).body_html == "<i>deep</i>"

    single = message("m2", "t2", sent_at=SENT_AT, html=None, text=None)
    single["payload"] = {
        "mimeType": "text/plain",
        "headers": single["payload"]["headers"],
        "body": {"data": b64("plain only")},
    }
    parsed = parse_message(single)
    assert (parsed.body_text, parsed.body_html) == ("plain only", None)


def test_missing_headers_default_to_empty():
    raw = message("m1", "t1", sent_at=SENT_AT)
    raw["payload"]["headers"] = []
    parsed = parse_message(raw)
    assert (parsed.from_addr, parsed.to_addr, parsed.subject) == ("", "", "")
