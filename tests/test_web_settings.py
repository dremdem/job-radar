from sqlalchemy import select

from job_radar.auto_ignore import load_rules
from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity, OpportunityState


def add(session, title, location, state=OpportunityState.NEW, reason=None):
    o = Opportunity(
        title=title,
        company=get_or_create_company(session, "Acme"),
        location=location,
        state=state,
        auto_ignore_reason=reason,
    )
    session.add(o)
    session.commit()
    return o


def test_settings_page_and_save(client, session):
    text = client.get("/settings").text
    assert 'name="hybrid"' in text and 'name="days" value="90"' in text
    response = client.post("/settings", data={"hybrid": "on", "applied": "on", "days": "60"})
    assert response.status_code == 200
    rules = load_rules(session)
    assert (rules.hybrid, rules.applied, rules.ignored, rules.rejected, rules.days) == (
        True,
        True,
        False,
        False,
        60,
    )
    assert "Saved" in response.text


def test_apply_now_from_settings(client, session):
    add(session, "Hybrid role", "Berlin (Hybrid)")
    client.post("/settings", data={"hybrid": "on", "days": "90"})
    response = client.post("/settings/apply")
    assert "1 vacancy auto-ignored" in response.text
    assert session.scalar(select(Opportunity.state)) is OpportunityState.AUTO_IGNORE


def test_auto_ignored_tab_shows_reason_and_undo_clears_it(client, session):
    o = add(
        session, "Hybrid role", "Berlin (Hybrid)", OpportunityState.AUTO_IGNORE, "hybrid / on-site"
    )
    review = client.get("/").text
    assert "Hybrid role" not in review and "Auto-ignored (1)" in review
    text = client.get("/?tab=auto").text
    assert "Hybrid role" in text and "auto-ignored: hybrid / on-site" in text
    client.post(f"/opportunities/{o.id}/state", data={"state": "interested", "tab": "auto"})
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (OpportunityState.INTERESTED, None)


def test_nav_links_to_settings(client):
    assert 'href="/settings"' in client.get("/").text


def test_countries_are_edited_on_settings(client, session):
    from job_radar.auto_ignore import load_rules

    text = client.get("/settings").text
    assert 'name="countries"' in text and "India, Pakistan" in text
    client.post("/settings", data={"countries": " india ,Pakistan,\nVietnam, ", "days": "90"})
    assert load_rules(session).countries == ("India", "Pakistan", "Vietnam")
    assert "India, Pakistan, Vietnam" in client.get("/settings").text
    client.post("/settings", data={"countries": "", "days": "90"})
    assert load_rules(session).countries == ()
