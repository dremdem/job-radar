"""Tests for undoing the last decision in the queue: the log, the confirmation text, the
undo itself and the routes behind Z (#56, docs/queue.md#undo).
"""

import datetime as dt

import pytest
import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.review.undo as review_undo

BEFORE = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)


def vacancy(session, title="Backend Engineer", state=models.OpportunityState.NEW, reason=None):
    o = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, "Acme"),
        state=state,
        state_changed_at=BEFORE,
        auto_ignore_reason=reason,
    )
    session.add(o)
    session.commit()
    return o


def as_utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=dt.UTC)


def test_a_decision_is_logged_and_undone(session):
    o = vacancy(session)
    action = review_undo.change_state(session, o, models.OpportunityState.APPLIED)
    session.commit()
    last = review_undo.last_action(session)
    assert last.action.id == action.id
    assert last.text == "applied · Backend Engineer — Acme"
    review_undo.undo(session, action.id)
    session.refresh(o)
    assert o.state is models.OpportunityState.NEW
    assert as_utc(o.state_changed_at) == BEFORE
    assert review_undo.last_action(session) is None


def test_undo_brings_back_the_auto_ignore_reason(session):
    o = vacancy(session, state=models.OpportunityState.AUTO_IGNORE, reason="location")
    action = review_undo.change_state(session, o, models.OpportunityState.INTERESTED)
    session.commit()
    assert o.auto_ignore_reason is None
    review_undo.undo(session, action.id)
    assert o.state is models.OpportunityState.AUTO_IGNORE
    assert o.auto_ignore_reason == "location"


def test_undo_of_an_ignore_reason_deletes_the_reason(session):
    o = vacancy(session)
    action = review_undo.ignore_with_reason(session, o, models.NoteKind.SALARY, comment="too low")
    assert review_undo.last_action(session).text.startswith("ignore (salary · too low) · ")
    review_undo.undo(session, action.id)
    assert o.state is models.OpportunityState.NEW
    assert session.scalars(sa.select(models.OpportunityNote)).all() == []


def test_a_bad_reason_logs_nothing(session):
    o = vacancy(session)
    with pytest.raises(ValueError):
        review_undo.ignore_with_reason(session, o, models.NoteKind.ONLY_LOCATION)
    assert review_undo.last_action(session) is None


def test_z_again_offers_the_action_before(session):
    first, second = vacancy(session, title="First"), vacancy(session, title="Second")
    review_undo.change_state(session, first, models.OpportunityState.IGNORE)
    session.commit()
    action = review_undo.change_state(session, second, models.OpportunityState.APPLIED)
    session.commit()
    review_undo.undo(session, action.id)
    assert review_undo.last_action(session).opportunity.id == first.id


def test_undo_refuses_when_the_vacancy_changed_since(session):
    o = vacancy(session)
    action = review_undo.change_state(session, o, models.OpportunityState.APPLIED)
    session.commit()
    # changed by something else (a sync rule, a merge), not logged
    o.state = models.OpportunityState.IGNORE
    session.commit()
    with pytest.raises(review_undo.UndoConflict):
        review_undo.undo(session, action.id)
    assert o.state is models.OpportunityState.IGNORE


def test_state_route_logs_and_undo_route_undoes(client, session):
    o = vacancy(session)
    client.post(f"/opportunities/{o.id}/state", data={"state": "applied", "from": "panel"})
    last = client.get("/undo").json()
    assert last["opportunity"] == o.id and last["text"].startswith("applied · ")
    response = client.post("/undo", data={"action": last["action"]})
    assert response.json() == {"opportunity": o.id, "state": "new"}
    assert client.get("/undo").json() == {"action": None}
    # the same action again: already undone
    assert client.post("/undo", data={"action": last["action"]}).status_code == 404


def test_ignore_reason_route_is_logged(client, session):
    o = vacancy(session)
    client.post(
        f"/opportunities/{o.id}/notes",
        data={"action": "ignore", "kind": "stack", "detail": "Java", "from": "panel"},
    )
    assert client.get("/undo").json()["text"].startswith("ignore (stack: Java) · ")


def test_undo_route_conflict_is_409(client, session):
    o = vacancy(session)
    client.post(f"/opportunities/{o.id}/state", data={"state": "applied", "from": "panel"})
    action = client.get("/undo").json()["action"]
    o.state = models.OpportunityState.IGNORE
    session.commit()
    response = client.post("/undo", data={"action": action})
    assert response.status_code == 409 and "changed after" in response.json()["detail"]


def test_queue_page_has_the_z_dialog(client, session):
    vacancy(session)
    page = client.get("/").text
    assert 'id="undo-dialog"' in page and "Z undo" in page
