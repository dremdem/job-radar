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
    # Only the row is replaced (by nothing), not the whole queue: the page keeps its scroll.
    assert "<table" not in response.text and 'hx-swap-oob="true"' in response.text


def test_state_buttons_target_their_own_row(client, session):
    rows = seed(session)
    text = client.get("/").text
    # Items are links; decisions live in the panel, which redraws itself and the list.
    assert f'<a class="item" id="opportunity-{rows["new"].id}"' in text
    panel = client.get(f"/opportunities/{rows['new'].id}/panel").text
    assert 'hx-target="#detail"' in panel


def test_row_staying_in_the_tab_comes_back_alone(client, session):
    rows = seed(session)
    response = client.post(
        f"/opportunities/{rows['interested'].id}/state",
        data={"state": "interested", "tab": "interested"},
    )
    assert f'id="opportunity-{rows["interested"].id}"' in response.text
    assert "<table" not in response.text


def test_full_rerenders_keep_the_scroll_position(client, session):
    seed(session)
    text = client.get("/").text
    # The list scrolls on its own; the selected item is brought into view.
    assert 'class="inbox-list"' in text and "scrollIntoView" in text


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
    assert 'class="col-filter" name="tab" value="interested"' in text
    assert f'id="opportunity-{rows["interested"].id}"' in text
    # The panel opens with the list's tab, so a decision redraws the right tab.
    assert "/panel?tab=" in text
    response = client.post(
        f"/opportunities/{rows['interested'].id}/state",
        data={"state": "ignore", "from": "panel", "tab": "interested"},
    ).text
    assert f'<a id="opportunity-{rows["interested"].id}" hx-swap-oob="delete">' in response


def test_empty_tab_message(client, session):
    add(session, "Fresh role")
    assert "Nothing here" in client.get("/?tab=applied").text


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
    text = client.get("/?q=tricon").text
    assert 'href="/?tab=interested&amp;q=tricon"' in text


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


def test_header_stays_when_nothing_matches(client, session):
    seed_places(session)
    text = client.get("/?q=nobody-at-all").text
    assert 'name="q" value="nobody-at-all"' in text
    assert 'id="queue-body"' in text and "Nothing here" in text


def test_queue_highlights_the_row_you_opened(client, session):
    add(session, "Fresh role")
    text = client.get("/").text
    assert ".item.selected" in text  # style for the selected item
    assert "jr-current-row" in text  # remembered in localStorage
    assert "htmx:afterSwap" in text
    assert "scrollIntoView" in text  # after a reload the item may have moved: bring it into view


def test_filter_by_title(client, session):
    seed_places(session)
    text = client.get("/?tab=all&title=BACKEND").text
    assert "Backend India" in text and "Backend London" in text
    assert "Python Poland" not in text and "Engineer Munich" not in text
    assert "All (2)" in text


def test_title_combines_with_other_filters_and_is_kept(client, session):
    seed_places(session)
    text = client.get("/?tab=all&title=backend&country=india").text
    assert "Backend India" in text and "Backend London" not in text
    assert 'name="title" value="backend"' in text
    assert 'href="/?tab=review&amp;title=backend&amp;country=india"' in text


def test_search_box_matches_title_company_and_place(client, session):
    seed_places(session)
    text = client.get("/?tab=all&q=india").text
    assert "Backend India" in text and "Backend London" not in text
    assert 'name="q" value="india"' in text
    assert 'hx-target="#queue-body"' in text and 'hx-select-oob="#tabs,#desc-switch"' in text


def test_keyboard_and_next_after_a_decision(client, session):
    add(session, "Fresh role")
    text = client.get("/").text
    assert 'key === "j"' in text and 'key === "k"' in text
    assert "data-state-key" in text  # I / X / A press the panel's buttons
    assert "nextElementSibling" in text  # after a decision the next vacancy opens


def test_long_item_text_wraps_instead_of_being_cut(client, session):
    add(session, "Fresh role", company="Logjec HR/Payroll Consultancy & Recruitment")
    text = client.get("/").text
    css = {line.split("{")[0].strip(): line for line in text.splitlines() if ".item-" in line}
    for name in (".item-title", ".item-meta"):
        assert "ellipsis" not in css[name] and "nowrap" not in css[name]


def test_capture_update_leaves_a_notice_when_the_vacancy_left_the_tab(client, session):
    o = add(session, "Senior Java Developer", company="Globex")
    o.state = OpportunityState.AUTO_IGNORE
    o.auto_ignore_reason = "title: java"
    session.commit()
    row = client.get(f"/opportunities/{o.id}/row?tab=review&notify=1").text
    assert 'class="item"' not in row  # it left To review
    assert 'hx-swap-oob="beforeend:#notices"' in row
    assert "Senior Java Developer" in row and "Globex" in row
    assert "moved to <b>Auto-ignored</b>" in row and "title: java" in row
    assert f'href="/opportunities/{o.id}"' in row and ">Close<" in row
    # Without notify (Claude updates) and while still in the tab: no notice.
    assert "#notices" not in client.get(f"/opportunities/{o.id}/row?tab=review").text
    assert "#notices" not in client.get(f"/opportunities/{o.id}/row?tab=auto&notify=1").text


def test_queue_has_the_notice_area_and_asks_for_notices_after_capture(client, session):
    add(session, "Fresh role")
    text = client.get("/").text
    assert 'id="notices"' in text and "notify=1" in text


def test_the_panel_decides_what_is_current_and_clears_after_the_last(client, session):
    add(session, "Fresh role")
    text = client.get("/").text
    # A redrawn item loses its highlight, so the next-after-a-decision logic reads the panel.
    assert 'document.querySelector("#detail .detail")' in text and "shownItem()" in text
    assert "Nothing left in this tab." in text and "localStorage.removeItem(KEY)" in text
    assert "#queue-body:not(:has(.item, p))::after" in text


def describe(session, o, text="We use Python."):
    from job_radar.dedup import add_snapshot, upsert_source_record
    from job_radar.models import SourceType

    record = upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    add_snapshot(session, record, text)
    session.commit()


def test_filter_by_description(client, session):
    bare = add(session, "Bare role")
    full = add(session, "Full role")
    describe(session, full)
    text = client.get("/?desc=none").text
    assert f'id="opportunity-{bare.id}"' in text and f'id="opportunity-{full.id}"' not in text
    assert "To review (2)" in text  # tab counts ignore the switch (#186)
    assert ">No description (1)<" in text and ">With description (1)<" in text
    assert ">All (2)<" in text
    assert 'class="col-filter" name="desc" value="none"' in text
    assert 'href="/?tab=interested&amp;desc=none"' in text  # tab links keep it
    text = client.get("/?desc=has").text
    assert f'id="opportunity-{full.id}"' in text and f'id="opportunity-{bare.id}"' not in text
    # The switch itself: three links, the current one marked.
    assert ">No description (1)<" in text and ">With description (1)<" in text
    assert 'aria-current="true"' in text
    # A decision in the panel keeps the view: the other item is not drawn into it.
    response = client.post(
        f"/opportunities/{full.id}/state",
        data={"state": "interested", "from": "panel", "tab": "review", "desc": "has"},
    ).text
    assert "To review (1)" in response


def test_queue_is_paged(client, session):
    for n in range(30):
        o = add(session, f"Role {n:02d}")
        o.score = n / 3
    session.commit()
    text = client.get("/?per=25").text
    assert text.count('class="item"') == 25
    assert "1\u201325 of 30" in text and 'href="/?tab=review&amp;per=25&amp;page=2"' in text
    assert "Role 29" in text and "Role 04" not in text  # sorted by score before paging
    page2 = client.get("/?per=25&page=2").text
    assert page2.count('class="item"') == 5 and "26\u201330 of 30" in page2 and "Role 04" in page2
    assert 'href="/?tab=review&amp;per=25&amp;page=1"' in page2
    # Per page is kept in tab links and in the list's filters; a new tab starts at page 1.
    assert 'href="/?tab=ignored&amp;per=25"' in page2
    assert 'class="col-filter" name="per" value="25"' in page2
    assert '<select class="form-select form-select-sm w-auto per-select"' in page2
    # 50 by default; a page past the end shows the last one.
    assert client.get("/").text.count('class="item"') == 30
    assert "26\u201330 of 30" in client.get("/?per=25&page=9").text


def test_search_redraws_the_description_switch(client, session):
    add(session, "Fresh role")
    text = client.get("/?q=altery").text
    # The switch links carry the search, so a new search must redraw them like the tabs.
    assert 'hx-select-oob="#tabs,#desc-switch"' in text
    assert '<nav id="desc-switch"' in text
    assert 'href="/?tab=review&amp;q=altery&amp;desc=none"' in text


def test_sort_by_last_action(client, session):
    from datetime import UTC, datetime, timedelta

    from job_radar.manual import add_event

    old = add(session, "Old role")
    old.score = 9.0
    fresh = add(session, "Fresh role two")
    fresh.score = 1.0
    session.commit()
    # Score order by default; Last action puts the one you touched first.
    text = client.get("/?tab=all").text
    assert text.index("Old role") < text.index("Fresh role two")
    add_event(session, fresh, "call", datetime.now(UTC) - timedelta(minutes=1), "recruiter")
    text = client.get("/?tab=all&sort=recent").text
    assert text.index("Fresh role two") < text.index("Old role")
    assert "· call</span>" in text  # what the last action was
    # A state change by you counts too, and is stamped.
    client.post(f"/opportunities/{old.id}/state", data={"state": "interested", "from": "panel"})
    session.refresh(old)
    assert old.state_changed_at is not None
    text = client.get("/?tab=all&sort=recent").text
    assert text.index("Old role") < text.index("Fresh role two")
    assert "· interested</span>" in text
    # The switch, kept in tab links and the list filters.
    assert 'href="/?tab=review&amp;sort=recent"' in text
    assert 'class="col-filter" name="sort" value="recent"' in text
    assert ">Last action<" in text and ">Score<" in text


def test_sort_by_claude_match(client, session):
    from job_radar.models import ClaudeAnalysis

    def analysed(title: str, score: float, match: int | None, state: str = "done"):
        o = add(session, title)
        o.score = score
        if match is not None or state != "done":
            result = {"verdict": "maybe", "match": match} if match is not None else None
            session.add(
                ClaudeAnalysis(
                    opportunity=o, model="m", prompt_version="v", state=state, result=result
                )
            )
        return o

    analysed("Top score role", 9.5, None)
    analysed("Low match role", 5.0, 30)
    analysed("High match role", 4.0, 90)
    analysed("Queued role", 8.0, None, state="queued")
    session.commit()
    text = client.get("/?tab=all&sort=claude").text
    order = ["High match role", "Low match role", "Top score role", "Queued role"]
    assert [text.index(t) for t in order] == sorted(text.index(t) for t in order)
    assert ">Claude %<" in text
    assert 'class="col-filter" name="sort" value="claude"' in text
