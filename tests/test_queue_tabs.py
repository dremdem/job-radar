from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity, OpportunityState


def add(session, title, state=OpportunityState.NEW, company="Acme"):
    o = Opportunity(title=title, company=get_or_create_company(session, company), state=state)
    session.add(o)
    session.commit()
    return o


def seed(session):
    return {
        "new": add(session, "Fresh role"),
        "interested": add(session, "Liked role", OpportunityState.INTERESTED),
        "applied": add(session, "Sent role", OpportunityState.APPLIED),
        "ignored": add(session, "Nope role", OpportunityState.IGNORE),
    }


def test_default_tab_is_to_review(client, session):
    seed(session)
    text = client.get("/").text
    assert "Fresh role" in text
    for other in ("Liked role", "Sent role", "Nope role"):
        assert other not in text
    assert "To review (1)" in text
    assert "Interested (1)" in text and "Applied (1)" in text and "Ignored (1)" in text
    assert "All (4)" in text


def test_each_tab_shows_its_state(client, session):
    seed(session)
    assert "Liked role" in client.get("/?tab=interested").text
    assert "Sent role" in client.get("/?tab=applied").text
    assert "Nope role" in client.get("/?tab=ignored").text
    everything = client.get("/?tab=all").text
    assert all(t in everything for t in ("Fresh role", "Liked role", "Sent role", "Nope role"))


def test_unknown_tab_falls_back_to_review(client, session):
    seed(session)
    assert "Fresh role" in client.get("/?tab=bogus").text


def test_processed_row_leaves_the_tab_at_once(client, session):
    rows = seed(session)
    response = client.post(
        f"/opportunities/{rows['new'].id}/state", data={"state": "applied", "tab": "review"}
    )
    assert response.status_code == 200
    assert "Fresh role" not in response.text
    assert "To review (0)" in response.text and "Applied (2)" in response.text


def test_all_tab_keeps_the_row_and_updates_counts(client, session):
    rows = seed(session)
    response = client.post(
        f"/opportunities/{rows['new'].id}/state", data={"state": "interested", "tab": "all"}
    )
    text = response.text
    assert f'id="opportunity-{rows["new"].id}"' in text
    assert "Liked role" not in text  # only the changed row comes back
    assert 'hx-swap-oob="true"' in text and "Interested (2)" in text


def test_buttons_carry_the_current_tab(client, session):
    rows = seed(session)
    text = client.get("/?tab=interested").text
    assert f'hx-post="/opportunities/{rows["interested"].id}/state"' in text
    assert '"tab": "interested"' in text
    assert 'name="tab" value="interested"' in text


def test_empty_tab_message(client, session):
    add(session, "Fresh role")
    assert "Nothing here" in client.get("/?tab=applied").text
