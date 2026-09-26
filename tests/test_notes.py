from datetime import UTC, datetime, timedelta

import pytest

from job_radar.contacts import company_history
from job_radar.dedup import get_or_create_company
from job_radar.models import NoteKind, Opportunity, OpportunityState
from job_radar.notes import (
    add_note,
    country_from_location,
    delete_note,
    ignore_with_reason,
    location_penalty,
)


@pytest.mark.parametrize(
    ("location", "country"),
    [
        ("Lucknow, Uttar Pradesh, India", "India"),
        ("United Kingdom", "United Kingdom"),
        ("Germany (Remote)", "Germany"),
        ("Berlin, Germany (Hybrid)", "Germany"),
        (None, None),
        ("", None),
    ],
)
def test_country_from_location(location, country):
    assert country_from_location(location) == country


def opp(
    session, company="Tricon Solutions", location="Lucknow, Uttar Pradesh, India", title="Sr Python"
):
    o = Opportunity(title=title, company=get_or_create_company(session, company), location=location)
    session.add(o)
    session.commit()
    return o


def test_ignore_with_reason_sets_state_and_stores_note(session):
    o = opp(session)
    note = ignore_with_reason(session, o, NoteKind.ONLY_LOCATION, "India", "residents only")
    assert o.state is OpportunityState.IGNORE
    assert (note.kind, note.detail, note.comment, note.company_id) == (
        NoteKind.ONLY_LOCATION,
        "India",
        "residents only",
        o.company_id,
    )


def test_note_does_not_change_state(session):
    o = opp(session)
    o.state = OpportunityState.INTERESTED
    add_note(session, o, NoteKind.NOTE, comment="ask about EOR")
    assert o.state is OpportunityState.INTERESTED


def test_note_needs_text():
    with pytest.raises(ValueError):
        add_note(None, None, NoteKind.NOTE, comment="  ")


def test_location_penalty_applies_to_other_vacancies_of_the_company(session):
    ignored = opp(session)
    ignore_with_reason(session, ignored, NoteKind.ONLY_LOCATION, "India")
    same_country = opp(session, location="Pune, India")
    elsewhere = opp(session, location="Poland")
    other_company = opp(session, company="Other Co")

    assert location_penalty(session, ignored) is None  # its own reason, already ignored
    points, reason = location_penalty(session, same_country)
    assert points == -3.0
    assert reason.text.startswith("Last time Tricon Solutions hired only in India (")
    assert location_penalty(session, elsewhere)[0] == -1.5
    assert location_penalty(session, other_company) is None


def test_only_latest_location_reason_counts(session):
    first = opp(session)
    note = ignore_with_reason(session, first, NoteKind.ONLY_LOCATION, "India")
    note.created_at = datetime.now(UTC) - timedelta(days=30)
    second = opp(session, location="United Kingdom")
    ignore_with_reason(session, second, NoteKind.ONLY_LOCATION, "United Kingdom")
    new = opp(session, location="London, United Kingdom")
    points, reason = location_penalty(session, new)
    assert points == -3.0 and "United Kingdom" in reason.text


def test_deleting_the_reason_removes_the_penalty(session):
    ignored = opp(session)
    note = ignore_with_reason(session, ignored, NoteKind.ONLY_LOCATION, "India")
    other = opp(session, location="India")
    delete_note(session, note.id)
    assert location_penalty(session, other) is None


def test_other_reasons_do_not_penalize(session):
    ignore_with_reason(session, opp(session), NoteKind.SALARY, comment="too low")
    assert location_penalty(session, opp(session)) is None


def test_reasons_and_notes_in_company_history(session):
    o = opp(session)
    ignore_with_reason(session, o, NoteKind.ONLY_LOCATION, "India", "residents only")
    add_note(session, o, NoteKind.NOTE, comment="recruiter was nice")
    events = company_history(session, o.company_id)
    texts = {(e.kind, e.text) for e in events}
    assert ("ignored", "only India · residents only") in texts
    assert ("note", "recruiter was nice") in texts
    assert all(e.note_id for e in events)


def test_language_reason(session):
    from job_radar.notes import describe

    o = opp(session, title="Desarrollador Python")
    note = ignore_with_reason(session, o, NoteKind.LANGUAGE, "Spanish", "not for me")
    assert note.detail == "Spanish"
    assert describe(note) == "language: Spanish · not for me"
    assert location_penalty(session, opp(session)) is None  # not a location reason
