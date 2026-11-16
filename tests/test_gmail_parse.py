"""Tests for job_radar.sources.gmail.parse: headers, dates, direction and bodies of Gmail API
messages (docs/gmail.md).
"""

import datetime as dt

import gmail_fakes

import job_radar.models as models
import job_radar.sources.gmail.parse as gmail_parse

SENT_AT = dt.datetime(2026, 9, 1, 10, 30, tzinfo=dt.UTC)


def test_parse_headers_and_date():
    parsed = gmail_parse.parse_message(
        gmail_fakes.message("m1", "t1", sent_at=SENT_AT, subject="Hi")
    )
    assert parsed.gmail_message_id == "m1"
    assert parsed.gmail_thread_id == "t1"
    assert parsed.sent_at == SENT_AT
    assert parsed.from_addr == "Recruiter <jane@acme.example>"
    assert parsed.to_addr == "me@example.com"
    assert parsed.subject == "Hi"
    assert parsed.snippet == "Hello there"
    assert parsed.direction is models.EmailDirection.IN


def test_sent_label_means_outgoing():
    parsed = gmail_parse.parse_message(
        gmail_fakes.message("m1", "t1", sent_at=SENT_AT, labels=("SENT",))
    )
    assert parsed.direction is models.EmailDirection.OUT


def test_extracts_html_and_text_bodies():
    parsed = gmail_parse.parse_message(
        gmail_fakes.message("m1", "t1", sent_at=SENT_AT, html="<p>Ол</p>", text="Ол")
    )
    assert parsed.body_html == "<p>Ол</p>"
    assert parsed.body_text == "Ол"


def test_nested_parts_and_single_part_body():
    raw = gmail_fakes.message("m1", "t1", sent_at=SENT_AT, html=None, text=None)
    raw["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html", "body": {"data": gmail_fakes.b64("<i>deep</i>")}}],
        },
        {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "x"}},
    ]
    assert gmail_parse.parse_message(raw).body_html == "<i>deep</i>"

    single = gmail_fakes.message("m2", "t2", sent_at=SENT_AT, html=None, text=None)
    single["payload"] = {
        "mimeType": "text/plain",
        "headers": single["payload"]["headers"],
        "body": {"data": gmail_fakes.b64("plain only")},
    }
    parsed = gmail_parse.parse_message(single)
    assert (parsed.body_text, parsed.body_html) == ("plain only", None)


def test_missing_headers_default_to_empty():
    raw = gmail_fakes.message("m1", "t1", sent_at=SENT_AT)
    raw["payload"]["headers"] = []
    parsed = gmail_parse.parse_message(raw)
    assert (parsed.from_addr, parsed.to_addr, parsed.subject) == ("", "", "")
