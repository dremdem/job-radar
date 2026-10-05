from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity, OpportunityState


def add_opportunity(
    session, title="Senior Python Engineer", company="Acme", score=None
) -> Opportunity:
    opportunity = Opportunity(
        title=title, company=get_or_create_company(session, company), score=score
    )
    session.add(opportunity)
    session.commit()
    return opportunity


def test_empty_queue(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No opportunities yet" in response.text


def test_queue_lists_opportunities(client, session):
    add_opportunity(session)
    response = client.get("/")
    assert "Senior Python Engineer" in response.text
    assert "Acme" in response.text


def test_set_state(client, session):
    opportunity = add_opportunity(session)
    response = client.post(f"/opportunities/{opportunity.id}/state", data={"state": "interested"})
    assert response.status_code == 200
    assert "interested" in response.text
    session.refresh(opportunity)
    assert opportunity.state is OpportunityState.INTERESTED


def test_set_state_rejects_unknown_state(client, session):
    opportunity = add_opportunity(session)
    response = client.post(f"/opportunities/{opportunity.id}/state", data={"state": "hired"})
    assert response.status_code == 422


def test_set_state_unknown_opportunity(client):
    response = client.post("/opportunities/999/state", data={"state": "ignore"})
    assert response.status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_queue_shows_details_and_sorts_by_score(client, session):
    from job_radar.dedup import upsert_source_record
    from job_radar.models import SourceType

    low = add_opportunity(session, title="Low match")
    low.score = 2
    high = add_opportunity(session, title="High match")
    high.score = 9
    high.location = "Worldwide (Remote)"
    high.easy_apply = True
    high.score_reasons = [{"sign": "+", "text": "Python"}, {"sign": "-", "text": "Frontend"}]
    upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="42",
        url="https://www.linkedin.com/jobs/view/42/",
        opportunity=high,
    )
    session.commit()

    text = client.get("/").text
    assert text.index("High match") < text.index("Low match")
    assert "Worldwide (Remote)" in text
    assert "Easy Apply" in text
    assert "+ Python" in text and "\u2212 Frontend" in text
    assert 'href="https://www.linkedin.com/jobs/view/42/"' in text
    assert "verify manually" in text


def test_reject_flags_all_company_opportunities(client, session):
    one = add_opportunity(session, title="Python Engineer", company="Globex", score=9)
    add_opportunity(session, title="Backend Engineer", company="Globex", score=8)
    add_opportunity(session, title="Other role", company="Initech", score=5)

    response = client.post(f"/opportunities/{one.id}/rejected")
    assert response.status_code == 200
    text = response.text
    # Both Globex rows are flagged, Initech is not.
    assert text.count("rejected ·") == 2
    # Penalised score is shown (9 - 5 = 4.0), not the intrinsic 9.
    assert "4.0" in text
    assert "Rejected by Globex" in text
    # The button now offers to undo.
    assert "un-reject" in text


def test_reject_sorts_to_bottom_but_stays_visible(client, session):
    rejected = add_opportunity(session, title="Great but rejected", company="Globex", score=10)
    add_opportunity(session, title="Modest match", company="Initech", score=3)

    client.post(f"/opportunities/{rejected.id}/rejected")
    text = client.get("/").text
    # Still listed, but below the lower-scored non-rejected one.
    assert "Great but rejected" in text
    assert text.index("Modest match") < text.index("Great but rejected")


def test_reject_is_a_toggle(client, session):
    o = add_opportunity(session, title="Python Engineer", company="Globex", score=9)
    client.post(f"/opportunities/{o.id}/rejected")
    text = client.post(f"/opportunities/{o.id}/rejected").text
    assert "rejected ·" not in text
    assert "un-reject" not in text


def test_queue_shows_company_history(client, session):
    from datetime import UTC, datetime

    from job_radar.models import Application, ApplicationReview, ApplicationSource

    known = add_opportunity(session, title="Known company role")
    session.add(
        Application(
            company_id=known.company_id,
            applied_at=datetime(2026, 3, 12, tzinfo=UTC),
            role="Python Developer",
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    text = client.get("/").text
    assert "contacted · 1" in text
    assert "already applied 2026-03-12" in text


def test_queue_says_no_contacts(client, session):
    add_opportunity(session, title="Fresh company role")
    assert "no contacts found" in client.get("/").text


def test_queue_rows_have_state_buttons(client, session):
    opportunity = add_opportunity(session)
    text = client.get("/").text
    assert f'hx-post="/opportunities/{opportunity.id}/state"' in text
    for state in ("interested", "ignore", "applied"):
        assert f'hx-vals=\'{{"state": "{state}", "tab": "review"}}\'' in text


def test_history_is_expanded_and_can_be_toggled(client, session):
    from datetime import UTC, datetime

    from job_radar.models import Application, ApplicationReview, ApplicationSource

    known = add_opportunity(session)
    session.add(
        Application(
            company_id=known.company_id,
            applied_at=datetime(2026, 3, 12, tzinfo=UTC),
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    text = client.get("/").text
    assert '<details class="history small" open>' in text
    assert 'id="expand-all"' in text and 'id="collapse-all"' in text


def test_public_search_jobs_are_marked(client, session):
    from job_radar.dedup import upsert_source_record
    from job_radar.models import SourceType

    o = add_opportunity(session, title="Found by search")
    upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_SEARCH,
        external_id="7",
        url="https://www.linkedin.com/jobs/view/7/",
        opportunity=o,
    )
    session.commit()
    assert '<span class="badge">public search</span>' in client.get("/").text


def test_ignore_with_reason_from_the_queue(client, session):
    from job_radar.models import OpportunityNote, OpportunityState

    o = add_opportunity(session, title="India only role")
    o.location = "Lucknow, Uttar Pradesh, India"
    other = add_opportunity(session, title="Another role")
    other.score = 8.0
    session.commit()
    text = client.get("/").text
    assert 'name="detail" value="India"' in text

    response = client.post(
        f"/opportunities/{o.id}/notes",
        data={
            "action": "ignore",
            "kind": "only_location",
            "detail": "India",
            "comment": "residents",
        },
    )
    assert response.status_code == 200
    session.refresh(o)
    assert o.state is OpportunityState.IGNORE
    assert "only India · residents" in response.text
    # The other vacancy of the same company now carries the risk and a lower score.
    assert "Last time Acme hired only in India" in response.text
    assert ">6.5<" in response.text

    note = session.query(OpportunityNote).one()
    after = client.post(f"/notes/{note.id}/delete").text
    assert "Last time Acme hired only in India" not in after


def test_add_note_from_the_queue(client, session):
    o = add_opportunity(session)
    response = client.post(
        f"/opportunities/{o.id}/notes",
        data={"action": "note", "kind": "note", "comment": "ask about EOR"},
    )
    assert response.status_code == 200
    assert "ask about EOR" in response.text


def test_empty_note_is_rejected(client, session):
    o = add_opportunity(session)
    response = client.post(
        f"/opportunities/{o.id}/notes", data={"action": "note", "kind": "note", "comment": ""}
    )
    assert response.status_code == 422


def test_queue_shows_language_risk(client, session):
    o = add_opportunity(session, title="Desarrollador Python Senior")
    o.score = 8.0
    session.commit()
    text = client.get("/").text
    assert "Title in Spanish: the job likely needs Spanish" in text
    assert ">0.0<" in text
    assert '<option value="language">' in text


def test_server_migrates_the_database_on_startup(tmp_path):
    from fastapi.testclient import TestClient

    from job_radar.config import Settings
    from job_radar.web.app import create_app

    url = f"sqlite:///{tmp_path / 'fresh.db'}"  # never migrated
    with TestClient(create_app(Settings(database_url=url))) as fresh:
        assert fresh.get("/settings").status_code == 200
        assert fresh.get("/").status_code == 200


def test_queue_shows_python_check_from_description(client, session):
    from job_radar.dedup import add_snapshot, upsert_source_record
    from job_radar.models import SourceType

    good = add_opportunity(session, title="Backend Engineer A")
    bad = add_opportunity(session, title="Backend Engineer B")
    bad.score = 8.0
    for o, text in ((good, "Strong Python 3.10+ and FastAPI."), (bad, "Go and Rust only.")):
        record = upsert_source_record(
            session,
            source_type=SourceType.LINKEDIN_CAPTURE,
            external_id=str(o.id),
            url=None,
            opportunity=o,
        )
        add_snapshot(session, record, text)
    session.commit()
    text = client.get("/").text
    assert "Python in description (x1; also FastAPI)" in text
    assert "Strong Python 3.10+" in text  # the quote
    assert "No Python in description" in text and ">5.0<" in text


def test_settings_offers_no_python_rule(client):
    assert 'name="no_python"' in client.get("/settings").text


def test_himalayas_jobs_are_attributed(client, session):
    from job_radar.dedup import add_snapshot, upsert_source_record
    from job_radar.models import SourceType

    o = add_opportunity(session, title="Python Developer")
    url = "https://himalayas.app/companies/acme/jobs/python-developer"
    record = upsert_source_record(
        session, source_type=SourceType.HIMALAYAS, external_id=url, url=url, opportunity=o
    )
    add_snapshot(session, record, "Python and Django.")
    session.commit()
    text = client.get("/").text
    assert '<span class="badge">via Himalayas</span>' in text
    assert f'href="{url}"' in text and ">open on himalayas.app<" in text


def test_settings_offers_language_rule(client):
    assert 'name="language"' in client.get("/settings").text


def test_queue_rows_carry_phone_card_hooks(client, session):
    from job_radar.models import OpportunityState

    o = add_opportunity(session, score=7.5)
    o.score_reasons = [{"sign": "+", "text": "Python"}]
    session.commit()
    text = client.get("/").text
    assert "@media (max-width: 720px)" in text
    assert 'class="cell-title"' in text and 'data-label="Score"' in text
    assert '<details class="why"' in text and "Why 7.5" in text
    page = client.get(f"/opportunities/{o.id}").text
    assert f'hx-post="/opportunities/{o.id}/state"' in page
    response = client.post(
        f"/opportunities/{o.id}/state", data={"state": "interested", "from": "page"}
    )
    assert response.status_code == 200
    session.refresh(o)
    assert o.state is OpportunityState.INTERESTED


def test_applications_rows_have_labels(client, session):
    from datetime import UTC, datetime

    from job_radar.models import Application, ApplicationReview, ApplicationSource

    o = add_opportunity(session)
    session.add(Application(company_id=o.company_id, applied_at=datetime(2026, 3, 1, tzinfo=UTC),
                            role="Dev", source=ApplicationSource.MANUAL,
                            review=ApplicationReview.CONFIRMED))  # fmt: skip
    session.commit()
    assert 'data-label="Company"' in client.get("/applications").text


def test_bootstrap_look(client, session):
    o = add_opportunity(session)
    text = client.get("/").text
    assert "bootstrap@5.3" in text and "bsTheme" in text
    assert 'class="navbar' in text and 'class="nav-link active"' in text
    assert "btn btn-sm btn-outline-danger" in text  # ignore
    assert "btn btn-sm btn-outline-success" in text  # interested
    assert 'class="tabs nav nav-pills' in text  # tabs
    page = client.get(f"/opportunities/{o.id}").text
    assert "btn btn-sm btn-outline-danger" in page
    assert 'class="nav-link active"' not in page or 'href="/" class="nav-link active"' in text


def test_actions_name_their_vacancy(client, session):
    add_opportunity(session, title="Senior Backend Developer")
    assert '<div class="actions-for small">▸ Senior Backend Developer</div>' in client.get("/").text


def test_reason_redraws_only_the_company_rows(client, session):
    o = add_opportunity(session, title="India only role", company="Globex")
    o.location = "Lucknow, India"
    sibling = add_opportunity(session, title="Another Globex role", company="Globex", score=8.0)
    other = add_opportunity(session, title="Initech role", company="Initech")
    session.commit()
    response = client.post(
        f"/opportunities/{o.id}/notes",
        data={"action": "ignore", "kind": "only_location", "detail": "India", "tab": "review"},
    ).text
    assert 'id="queue"' not in response  # the queue itself is not re-rendered
    assert f'id="opportunity-{o.id}"' not in response  # it left To review
    assert f'id="opportunity-{sibling.id}"' in response and 'hx-swap-oob="outerHTML"' in response
    assert "Last time Globex hired only in India" in response
    assert f'id="opportunity-{other.id}"' not in response
    assert 'id="tabs"' in response


def test_rejected_redraws_rows_not_the_queue(client, session):
    o = add_opportunity(session, title="Python Engineer", company="Globex", score=9)
    response = client.post(f"/opportunities/{o.id}/rejected", data={"tab": "review"}).text
    assert 'id="queue"' not in response and f'id="opportunity-{o.id}"' in response
    assert "un-reject" in response
