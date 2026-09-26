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


def test_country_has_its_own_column(client, session):
    o = add(session, "India role")
    o.location = "Lucknow, Uttar Pradesh, India"
    add(session, "No location role")
    session.commit()
    text = client.get("/").text
    assert "<th>Country</th>" in text
    assert '<td class="country">India</td>' in text
    assert '<td class="country">—</td>' in text


def seed_places(session):
    rows = {
        "tricon": add(session, "Backend India", company="Tricon Solutions"),
        "epam": add(session, "Python Poland", company="EPAM Anywhere"),
        "muller": add(session, "Engineer Munich", company="Müller GmbH"),
        "tricon_uk": add(
            session, "Backend London", OpportunityState.INTERESTED, company="Tricon Solutions"
        ),
    }
    rows["tricon"].location = "Lucknow, Uttar Pradesh, India"
    rows["epam"].location = "Poland (Remote)"
    rows["muller"].location = "München, Germany"
    rows["tricon_uk"].location = "London, United Kingdom"
    session.commit()
    return rows


def test_filter_by_company_is_partial_and_case_insensitive(client, session):
    seed_places(session)
    text = client.get("/?company=TRIC").text
    assert "Backend India" in text
    assert "Python Poland" not in text and "Engineer Munich" not in text


def test_filter_by_country_searches_the_location(client, session):
    seed_places(session)
    text = client.get("/?country=ind").text
    assert "Backend India" in text and "Python Poland" not in text
    assert "Engineer Munich" in client.get("/?country=MÜNCH").text  # non-ASCII case folding


def test_filters_combine_and_counts_follow(client, session):
    seed_places(session)
    text = client.get("/?tab=all&company=tricon&country=united").text
    assert "Backend London" in text and "Backend India" not in text
    assert "To review (0)" in text and "Interested (1)" in text and "All (1)" in text


def test_tab_links_keep_filters(client, session):
    seed_places(session)
    text = client.get("/?company=tricon&country=india").text
    assert 'href="/?tab=interested&amp;country=india&amp;company=tricon"' in text
    assert 'name="company" value="tricon"' in text and 'name="country" value="india"' in text


def test_buttons_keep_filters(client, session):
    rows = seed_places(session)
    text = client.get("/?company=tricon").text
    assert 'hx-include=".col-filter"' in text
    response = client.post(
        f"/opportunities/{rows['tricon'].id}/state",
        data={"state": "ignore", "tab": "review", "company": "tricon", "country": ""},
    )
    assert "Python Poland" not in response.text  # still filtered
    assert "To review (0)" in response.text


def test_filters_sit_in_the_table_header(client, session):
    seed_places(session)
    text = client.get("/?company=tricon").text
    thead = text[text.index("<thead>") : text.index("</thead>")]
    assert 'name="country"' in thead and 'name="company" value="tricon"' in thead
    assert 'hx-target="#queue-body"' in thead and 'hx-select-oob="#tabs"' in thead
    assert 'id="filters"' not in text  # the old form above the tabs is gone


def test_header_stays_when_nothing_matches(client, session):
    seed_places(session)
    text = client.get("/?company=nobody-at-all").text
    assert "<thead>" in text and 'name="company" value="nobody-at-all"' in text
    assert "Nothing here" in text


def test_queue_highlights_the_row_you_opened(client, session):
    add(session, "Fresh role")
    text = client.get("/").text
    assert "tr.current" in text  # style for the highlighted row
    assert "jr-current-row" in text  # remembered in localStorage
    assert "htmx:afterSwap" in text and "auxclick" in text
