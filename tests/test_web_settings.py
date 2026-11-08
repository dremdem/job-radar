from datetime import UTC, datetime

from sqlalchemy import select

from job_radar.auto_ignore import UNAVAILABLE_REASON, Rules, load_rules, reason_for
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


def test_claude_skip_reasons_are_chosen_on_settings(client, session):
    text = client.get("/settings").text
    assert 'name="claude_any"' in text and 'value="stack"' in text
    client.post(
        "/settings",
        data={"claude_kinds": ["location", "work_format"], "days": "90", "countries": "India"},
    )
    rules = load_rules(session)
    assert (rules.claude_any, rules.claude_kinds) == (False, ("location", "work_format"))
    text = client.get("/settings").text
    assert 'value="location" checked' in text and 'value="stack" checked' not in text
    client.post("/settings", data={"claude_any": "on", "claude_kinds": ["bogus"], "days": "90"})
    rules = load_rules(session)
    assert (rules.claude_any, rules.claude_kinds) == (True, ())


def test_unavailable_rule_is_on_by_default_and_can_be_switched_off(client, session):
    assert load_rules(session).unavailable
    text = client.get("/settings").text
    assert 'name="unavailable" checked' in text
    client.post("/settings", data={"days": "90"})
    assert not load_rules(session).unavailable
    assert 'name="unavailable" checked' not in client.get("/settings").text


def test_rule_moves_an_unavailable_vacancy(session):
    o = add(session, "Gone", "Remote")
    assert reason_for(session, o, Rules(countries=())) is None
    o.unavailable_at = datetime.now(UTC)
    assert reason_for(session, o, Rules(countries=())) == UNAVAILABLE_REASON
    assert reason_for(session, o, Rules(countries=(), unavailable=False)) is None


def test_unavailable_badge_replaces_verify_manually(client, session):
    o = add(session, "Gone", "Remote")
    assert "verify manually" in client.get(f"/opportunities/{o.id}").text
    o.unavailable_at = datetime.now(UTC)
    session.commit()
    page = client.get(f"/opportunities/{o.id}").text
    assert '<span class="badge reject">unavailable' in page
    assert "verify manually" not in page
    assert f'action="/opportunities/{o.id}/unavailable/clear"' in page
    item = client.get("/?tab=review").text
    assert "line-skip" in item and ">unavailable<" in item and "no description" not in item


def test_badge_button_clears_the_mark(client, session):
    o = add(session, "Gone", "Remote", OpportunityState.AUTO_IGNORE, UNAVAILABLE_REASON)
    o.unavailable_at = datetime.now(UTC)
    session.commit()
    response = client.post(f"/opportunities/{o.id}/unavailable/clear", follow_redirects=False)
    assert response.status_code == 303
    session.refresh(o)
    assert (o.unavailable_at, o.state, o.auto_ignore_reason) == (
        None,
        OpportunityState.NEW,
        None,
    )
