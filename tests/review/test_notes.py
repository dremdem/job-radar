"""Tests for job_radar.review.notes: reasons to ignore, free notes and the location penalty
for the company's other vacancies (docs/applications.md#reasons-and-notes).
"""

import datetime as dt

import pytest

import job_radar.dedup as dedup
import job_radar.history.contacts as history_contacts
import job_radar.models as models
import job_radar.review.notes as review_notes


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
    assert review_notes.country_from_location(location) == country


def opp(
    session, company="Tricon Solutions", location="Lucknow, Uttar Pradesh, India", title="Sr Python"
):
    o = models.Opportunity(
        title=title, company=dedup.get_or_create_company(session, company), location=location
    )
    session.add(o)
    session.commit()
    return o


def test_ignore_with_reason_sets_state_and_stores_note(session):
    o = opp(session)
    note = review_notes.ignore_with_reason(
        session, o, models.NoteKind.ONLY_LOCATION, "India", "residents only"
    )
    assert o.state is models.OpportunityState.IGNORE
    assert (note.kind, note.detail, note.comment, note.company_id) == (
        models.NoteKind.ONLY_LOCATION,
        "India",
        "residents only",
        o.company_id,
    )


def test_note_does_not_change_state(session):
    o = opp(session)
    o.state = models.OpportunityState.INTERESTED
    review_notes.add_note(session, o, models.NoteKind.NOTE, comment="ask about EOR")
    assert o.state is models.OpportunityState.INTERESTED


def test_note_needs_text():
    with pytest.raises(ValueError):
        review_notes.add_note(None, None, models.NoteKind.NOTE, comment="  ")


def test_location_penalty_applies_to_other_vacancies_of_the_company(session):
    ignored = opp(session)
    review_notes.ignore_with_reason(session, ignored, models.NoteKind.ONLY_LOCATION, "India")
    same_country = opp(session, location="Pune, India")
    elsewhere = opp(session, location="Poland")
    other_company = opp(session, company="Other Co")

    # its own reason, already ignored
    assert review_notes.location_penalty(session, ignored) is None
    points, reason = review_notes.location_penalty(session, same_country)
    assert points == -3.0
    assert reason.text.startswith("Last time Tricon Solutions hired only in India (")
    assert review_notes.location_penalty(session, elsewhere)[0] == -1.5
    assert review_notes.location_penalty(session, other_company) is None


def test_only_latest_location_reason_counts(session):
    first = opp(session)
    note = review_notes.ignore_with_reason(session, first, models.NoteKind.ONLY_LOCATION, "India")
    note.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    second = opp(session, location="United Kingdom")
    review_notes.ignore_with_reason(
        session, second, models.NoteKind.ONLY_LOCATION, "United Kingdom"
    )
    new = opp(session, location="London, United Kingdom")
    points, reason = review_notes.location_penalty(session, new)
    assert points == -3.0 and "United Kingdom" in reason.text


def test_deleting_the_reason_removes_the_penalty(session):
    ignored = opp(session)
    note = review_notes.ignore_with_reason(session, ignored, models.NoteKind.ONLY_LOCATION, "India")
    other = opp(session, location="India")
    review_notes.delete_note(session, note.id)
    assert review_notes.location_penalty(session, other) is None


def test_other_reasons_do_not_penalize(session):
    review_notes.ignore_with_reason(
        session, opp(session), models.NoteKind.SALARY, comment="too low"
    )
    assert review_notes.location_penalty(session, opp(session)) is None


def test_reasons_and_notes_in_company_history(session):
    o = opp(session)
    review_notes.ignore_with_reason(
        session, o, models.NoteKind.ONLY_LOCATION, "India", "residents only"
    )
    review_notes.add_note(session, o, models.NoteKind.NOTE, comment="recruiter was nice")
    events = history_contacts.company_history(session, o.company_id)
    texts = {(e.kind, e.text) for e in events}
    assert ("ignored", "only India · residents only") in texts
    assert ("note", "recruiter was nice") in texts
    assert all(e.note_id for e in events)


def test_language_reason(session):

    o = opp(session, title="Desarrollador Python")
    note = review_notes.ignore_with_reason(
        session, o, models.NoteKind.LANGUAGE, "Spanish", "not for me"
    )
    assert note.detail == "Spanish"
    assert review_notes.describe(note) == "language: Spanish · not for me"
    # not a location reason
    assert review_notes.location_penalty(session, opp(session)) is None
