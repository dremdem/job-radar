"""Tests for the Settings page: saving the auto-ignore rules, applying them now and
undoing an auto-ignore (docs/settings.md).
"""

import datetime as dt

import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore


def add(session, title, location, state=models.OpportunityState.NEW, reason=None):
    o = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, "Acme"),
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
    rules = review_auto_ignore.load_rules(session)
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
    assert (
        session.scalar(sa.select(models.Opportunity.state)) is models.OpportunityState.AUTO_IGNORE
    )


def test_auto_ignored_tab_shows_reason_and_undo_clears_it(client, session):
    o = add(
        session,
        "Hybrid role",
        "Berlin (Hybrid)",
        models.OpportunityState.AUTO_IGNORE,
        "hybrid / on-site",
    )
    review = client.get("/").text
    assert "Hybrid role" not in review and "Auto-ignored (1)" in review
    text = client.get("/?tab=auto").text
    assert "Hybrid role" in text and "auto-ignored: hybrid / on-site" in text
    client.post(f"/opportunities/{o.id}/state", data={"state": "interested", "tab": "auto"})
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (models.OpportunityState.INTERESTED, None)


def test_nav_links_to_settings(client):
    assert 'href="/settings"' in client.get("/").text


def test_countries_are_edited_on_settings(client, session):

    text = client.get("/settings").text
    assert 'name="countries"' in text and "India, Pakistan" in text
    client.post("/settings", data={"countries": " india ,Pakistan,\nVietnam, ", "days": "90"})
    assert review_auto_ignore.load_rules(session).countries == ("India", "Pakistan", "Vietnam")
    assert "India, Pakistan, Vietnam" in client.get("/settings").text
    client.post("/settings", data={"countries": "", "days": "90"})
    assert review_auto_ignore.load_rules(session).countries == ()


def test_claude_skip_reasons_are_chosen_on_settings(client, session):
    text = client.get("/settings").text
    assert 'name="claude_any"' in text and 'value="stack"' in text
    client.post(
        "/settings",
        data={"claude_kinds": ["location", "work_format"], "days": "90", "countries": "India"},
    )
    rules = review_auto_ignore.load_rules(session)
    assert (rules.claude_any, rules.claude_kinds) == (False, ("location", "work_format"))
    text = client.get("/settings").text
    assert 'value="location" checked' in text and 'value="stack" checked' not in text
    client.post("/settings", data={"claude_any": "on", "claude_kinds": ["bogus"], "days": "90"})
    rules = review_auto_ignore.load_rules(session)
    assert (rules.claude_any, rules.claude_kinds) == (True, ())


def test_unavailable_rule_is_on_by_default_and_can_be_switched_off(client, session):
    assert review_auto_ignore.load_rules(session).unavailable
    text = client.get("/settings").text
    assert 'name="unavailable" checked' in text
    client.post("/settings", data={"days": "90"})
    assert not review_auto_ignore.load_rules(session).unavailable
    assert 'name="unavailable" checked' not in client.get("/settings").text


def test_rule_moves_an_unavailable_vacancy(session):
    o = add(session, "Gone", "Remote")
    assert review_auto_ignore.reason_for(session, o, review_auto_ignore.Rules(countries=())) is None
    o.unavailable_at = dt.datetime.now(dt.UTC)
    assert (
        review_auto_ignore.reason_for(session, o, review_auto_ignore.Rules(countries=()))
        == review_auto_ignore.UNAVAILABLE_REASON
    )
    assert (
        review_auto_ignore.reason_for(
            session, o, review_auto_ignore.Rules(countries=(), unavailable=False)
        )
        is None
    )


def test_unavailable_badge_replaces_verify_manually(client, session):
    o = add(session, "Gone", "Remote")
    assert "verify manually" in client.get(f"/opportunities/{o.id}").text
    o.unavailable_at = dt.datetime.now(dt.UTC)
    session.commit()
    page = client.get(f"/opportunities/{o.id}").text
    assert '<span class="badge reject">unavailable' in page
    assert "verify manually" not in page
    assert f'action="/opportunities/{o.id}/unavailable/clear"' in page
    item = client.get("/?tab=review").text
    assert "line-skip" in item and ">unavailable<" in item and "no description" not in item


def test_badge_button_clears_the_mark(client, session):
    o = add(
        session,
        "Gone",
        "Remote",
        models.OpportunityState.AUTO_IGNORE,
        review_auto_ignore.UNAVAILABLE_REASON,
    )
    o.unavailable_at = dt.datetime.now(dt.UTC)
    session.commit()
    response = client.post(f"/opportunities/{o.id}/unavailable/clear", follow_redirects=False)
    assert response.status_code == 303
    session.refresh(o)
    assert (o.unavailable_at, o.state, o.auto_ignore_reason) == (
        None,
        models.OpportunityState.NEW,
        None,
    )


def test_not_accepting_badge_shows_with_a_description_too(client, session):
    o = add(session, "Closed", "Remote")
    o.not_accepting_at = dt.datetime.now(dt.UTC)
    session.commit()
    page = client.get(f"/opportunities/{o.id}").text
    assert '<span class="badge reject">not accepting' in page
    assert "verify manually" not in page
    assert f'action="/opportunities/{o.id}/unavailable/clear"' in page
    item = client.get("/?tab=review").text
    assert ">not accepting<" in item and "line-skip" in item


def test_rule_moves_a_vacancy_that_is_not_accepting(session):
    o = add(session, "Closed", "Remote")
    o.not_accepting_at = dt.datetime.now(dt.UTC)
    assert (
        review_auto_ignore.reason_for(session, o, review_auto_ignore.Rules(countries=()))
        == review_auto_ignore.NOT_ACCEPTING_REASON
    )
    assert (
        review_auto_ignore.reason_for(
            session, o, review_auto_ignore.Rules(countries=(), unavailable=False)
        )
        is None
    )


def test_stack_in_title_rule_is_edited_on_settings(client, session):
    text = client.get("/settings").text
    assert 'name="stack_title"' in text and 'name="stacks"' in text and "Ruby, Rails" in text
    client.post("/settings", data={"stack_title": "on", "stacks": "Ruby, Haskell", "days": "90"})
    rules = review_auto_ignore.load_rules(session)
    assert (rules.stack_title, rules.stacks) == (True, ("Ruby", "Haskell"))
    text = client.get("/settings").text
    assert 'name="stack_title" checked' in text and "Ruby, Haskell" in text
