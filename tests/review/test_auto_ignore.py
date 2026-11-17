"""Tests for job_radar.review.auto_ignore: every rule, the settings round trip, and that only
``new`` vacancies are moved (docs/settings.md).
"""

import datetime as dt

import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.history.rejections as history_rejections
import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.sources.linkedin.alerts as linkedin_alerts

NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.UTC)
ALL_ON = review_auto_ignore.Rules(hybrid=True, applied=True, ignored=True, rejected=True, days=90)


def opp(
    session,
    company="Capitole",
    title="Senior Python Developer",
    location="Spain (Remote)",
    state=models.OpportunityState.NEW,
):
    o = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, company),
        location=location,
        state=state,
    )
    session.add(o)
    session.commit()
    return o


def applied(session, company, at):
    session.add(
        models.Application(
            company_id=dedup.get_or_create_company(session, company).id,
            applied_at=at,
            source=models.ApplicationSource.LINKEDIN,
            review=models.ApplicationReview.CONFIRMED,
        )
    )
    session.commit()


def test_rules_are_off_by_default(session):
    rules = review_auto_ignore.load_rules(session)
    assert not (rules.hybrid or rules.applied or rules.ignored or rules.rejected)
    assert rules.days == 90
    assert (
        review_auto_ignore.reason_for(session, opp(session, location="Madrid (Hybrid)"), rules, NOW)
        is None
    )


def test_settings_round_trip(session):
    review_auto_ignore.save_rules(session, ALL_ON)
    assert review_auto_ignore.load_rules(session) == ALL_ON


def test_hybrid_rule(session):
    assert (
        review_auto_ignore.reason_for(
            session, opp(session, location="Berlin (Hybrid)"), ALL_ON, NOW
        )
        == "hybrid / on-site"
    )
    assert (
        review_auto_ignore.reason_for(
            session, opp(session, title="Backend Engineer (On-site)", location=None), ALL_ON, NOW
        )
        == "hybrid / on-site"
    )
    assert (
        review_auto_ignore.reason_for(
            session, opp(session, location="Germany (Remote)"), ALL_ON, NOW
        )
        is None
    )


def test_applied_rule_respects_the_window(session):
    applied(session, "Capitole", NOW - dt.timedelta(days=10))
    assert (
        review_auto_ignore.reason_for(session, opp(session), ALL_ON, NOW)
        == "applied to Capitole on 2026-09-17"
    )
    applied(session, "Old Co", NOW - dt.timedelta(days=200))
    assert (
        review_auto_ignore.reason_for(session, opp(session, company="Old Co"), ALL_ON, NOW) is None
    )


def test_applied_rule_counts_vacancies_you_marked_applied(session):
    # Sii Poland: one role in three cities, two marked applied in Job Radar, no mail (#211).
    katowice = opp(session, company="Sii Poland", state=models.OpportunityState.APPLIED)
    katowice.state_changed_at = NOW - dt.timedelta(days=3)
    session.commit()
    warsaw = opp(session, company="Sii Poland", location="Warsaw (Remote)")
    assert (
        review_auto_ignore.reason_for(session, warsaw, ALL_ON, NOW)
        == "applied to Sii Poland on 2026-09-24"
    )
    katowice.state_changed_at = NOW - dt.timedelta(days=200)
    session.commit()
    assert review_auto_ignore.reason_for(session, warsaw, ALL_ON, NOW) is None
    # The newer of the two counts: a confirmed application in the mail still wins.
    applied(session, "Sii Poland", NOW - dt.timedelta(days=1))
    assert (
        review_auto_ignore.reason_for(session, warsaw, ALL_ON, NOW)
        == "applied to Sii Poland on 2026-09-26"
    )


def test_ignored_rule_counts_only_your_own_ignore(session):
    first = opp(session, state=models.OpportunityState.IGNORE)
    first.updated_at = NOW - dt.timedelta(days=2)
    session.commit()
    assert (
        review_auto_ignore.reason_for(session, opp(session), ALL_ON, NOW)
        == "ignored another Capitole vacancy on 2026-09-25"
    )
    auto = opp(session, company="Auto Co", state=models.OpportunityState.AUTO_IGNORE)
    assert (
        review_auto_ignore.reason_for(session, opp(session, company="Auto Co"), ALL_ON, NOW) is None
    )
    assert auto.id


def test_rejected_rule_comes_first(session):
    company = dedup.get_or_create_company(session, "Capitole")
    history_rejections.mark_rejected(
        session, company_id=company.id, rejected_at=NOW - dt.timedelta(days=5)
    )
    applied(session, "Capitole", NOW - dt.timedelta(days=8))
    session.commit()
    assert (
        review_auto_ignore.reason_for(session, opp(session), ALL_ON, NOW)
        == "Capitole rejected you on 2026-09-22"
    )


def test_each_rule_can_be_switched_off(session):
    applied(session, "Capitole", NOW - dt.timedelta(days=10))
    rules = review_auto_ignore.Rules(
        hybrid=True, applied=False, ignored=True, rejected=True, days=90
    )
    assert review_auto_ignore.reason_for(session, opp(session), rules, NOW) is None


def test_new_vacancies_are_auto_ignored_on_import(session):
    review_auto_ignore.save_rules(session, ALL_ON)
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob("1", "Python Developer", "Acme", "Berlin (Hybrid)", None, False),
        models.SourceType.LINKEDIN_ALERT,
    )
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob("2", "Python Developer", "Acme", "Germany (Remote)", None, False),
        models.SourceType.LINKEDIN_ALERT,
    )
    session.commit()
    states = {
        o.location: (o.state, o.auto_ignore_reason)
        for o in session.scalars(sa.select(models.Opportunity))
    }
    assert states["Berlin (Hybrid)"] == (models.OpportunityState.AUTO_IGNORE, "hybrid / on-site")
    assert states["Germany (Remote)"] == (models.OpportunityState.NEW, None)


def test_apply_to_review_touches_only_new(session):
    hybrid_new = opp(session, location="Berlin (Hybrid)")
    hybrid_liked = opp(
        session, location="Munich (Hybrid)", state=models.OpportunityState.INTERESTED
    )
    review_auto_ignore.save_rules(session, ALL_ON)
    assert review_auto_ignore.apply_to_review(session, now=NOW) == 1
    for o in (hybrid_new, hybrid_liked):
        session.refresh(o)
    assert hybrid_new.state is models.OpportunityState.AUTO_IGNORE
    assert hybrid_liked.state is models.OpportunityState.INTERESTED


def with_description(session, text, title="Backend Engineer"):

    o = opp(session, title=title)
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    dedup.add_snapshot(session, record, text)
    session.commit()
    return o


def test_no_python_rule(session):
    rules = review_auto_ignore.Rules(no_python=True)
    assert review_auto_ignore.reason_for(
        session, with_description(session, "Go and Rust only."), rules, NOW
    ) == ("no Python in the description")
    assert (
        review_auto_ignore.reason_for(
            session, with_description(session, "Python and FastAPI."), rules, NOW
        )
        is None
    )
    # no description yet
    assert review_auto_ignore.reason_for(session, opp(session), rules, NOW) is None
    python_title = with_description(session, "Go only.", title="Python Developer")
    assert review_auto_ignore.reason_for(session, python_title, rules, NOW) is None


def test_description_from_the_bookmarklet_triggers_the_rule(session):
    import job_radar.sources.linkedin.capture as linkedin_capture

    review_auto_ignore.save_rules(session, review_auto_ignore.Rules(no_python=True))
    card = {"job_id": "501", "text": "Backend Engineer\nAcme\nGermany (Remote)"}
    linkedin_capture.import_capture(session, {"cards": [card]})
    o = session.scalar(sa.select(models.Opportunity))
    # no description yet
    assert o.state is models.OpportunityState.NEW
    linkedin_capture.import_capture(
        session, {"cards": [], "detail": {"job_id": "501", "description": "We use Go."}}
    )
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (
        models.OpportunityState.AUTO_IGNORE,
        "no Python in the description",
    )


def test_language_rule_reads_the_description(session):

    o = models.Opportunity(
        title="Senior Python Developer", company=dedup.get_or_create_company(session, "Acme")
    )
    session.add(o)
    session.flush()
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_CAPTURE,
        external_id="1",
        url=None,
        opportunity=o,
    )
    dedup.add_snapshot(
        session,
        record,
        "Wir suchen einen erfahrenen Entwickler für unser Team. Du arbeitest mit Python "
        "und Django an der Plattform, die von unseren Kunden jeden Tag genutzt wird.",
    )
    session.commit()
    session.refresh(o)
    assert review_auto_ignore.reason_for(session, o, review_auto_ignore.Rules()) is None
    assert (
        review_auto_ignore.reason_for(session, o, review_auto_ignore.Rules(language=True))
        == "language: German"
    )


def test_country_rule_is_on_with_india_and_pakistan_by_default(session):
    rules = review_auto_ignore.load_rules(session)
    assert rules.countries == ("India", "Pakistan") and rules.any
    for location in (
        "India (Remote)",
        "Bengaluru, Karnataka, India",
        "Pakistan",
        # LinkedIn often names only the city
        "Bengaluru (Remote)",
        "Mumbai Metropolitan Region (Hybrid)",
        "Greater Delhi Area",
        "Lahore (Remote)",
        "Karnataka, India (Remote)",
    ):
        reason = review_auto_ignore.reason_for(session, opp(session, location=location), rules, NOW)
        assert reason is not None and reason.startswith("country: "), location
    assert review_auto_ignore.reason_for(
        session, opp(session, location="Lahore (Remote)"), rules, NOW
    ) == ("country: Pakistan (Lahore (Remote))")
    for location in ("Indiana, United States (Remote)", "Spain (Remote)", None, ""):
        assert (
            review_auto_ignore.reason_for(session, opp(session, location=location), rules, NOW)
            is None
        ), location


def test_country_list_is_yours_and_empty_turns_it_off(session):
    vietnam = review_auto_ignore.Rules(countries=("Vietnam",))
    assert review_auto_ignore.reason_for(
        session, opp(session, location="Hanoi, Vietnam"), vietnam, NOW
    ) == ("country: Vietnam (Hanoi, Vietnam)")
    assert (
        review_auto_ignore.reason_for(
            session, opp(session, location="India (Remote)"), vietnam, NOW
        )
        is None
    )
    # the other rule that is on by default (#276)
    off = review_auto_ignore.Rules(countries=(), unavailable=False)
    assert not off.any
    assert (
        review_auto_ignore.reason_for(session, opp(session, location="India (Remote)"), off, NOW)
        is None
    )
    review_auto_ignore.save_rules(session, vietnam)
    # a tuple again after the JSON round trip
    assert review_auto_ignore.load_rules(session) == vietnam


def test_new_india_vacancy_goes_to_auto_ignored_on_import(session):
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob(
            "9001", "Python Developer", "Infosys", "Hyderabad (Remote)", None, False
        ),
        models.SourceType.LINKEDIN_ALERT,
    )
    session.commit()
    o = session.scalars(sa.select(models.Opportunity)).one()
    assert o.state is models.OpportunityState.AUTO_IGNORE
    assert o.auto_ignore_reason == "country: India (Hyderabad (Remote))"


def claude_says(session, o, verdict="skip", kind="location", detail="EU only"):

    result = {"verdict": verdict, "match": 20, "summary": "Нет."}
    if kind is not None:
        result["ignore_reason"] = {"kind": kind, "detail": detail}
    session.add(
        models.ClaudeAnalysis(
            opportunity_id=o.id, model="sonnet", prompt_version="3", state="done", result=result
        )
    )
    session.commit()


def test_claude_skip_rule_only_for_the_chosen_reasons(session):
    rules = review_auto_ignore.Rules(countries=(), claude_kinds=("location", "work_format"))
    eu, hybrid, stack = opp(session), opp(session, company="B"), opp(session, company="C")
    claude_says(session, eu, kind="location", detail="EU only")
    claude_says(session, hybrid, kind="work_format", detail="hybrid London")
    claude_says(session, stack, kind="stack", detail="C# .NET")
    assert review_auto_ignore.reason_for(session, eu, rules, NOW) == "Claude: location: EU only"
    assert (
        review_auto_ignore.reason_for(session, hybrid, rules, NOW)
        == "Claude: hybrid / on-site: hybrid London"
    )
    # stack: you look yourself
    assert review_auto_ignore.reason_for(session, stack, rules, NOW) is None
    # off by default
    assert (
        review_auto_ignore.reason_for(session, eu, review_auto_ignore.Rules(countries=()), NOW)
        is None
    )


def test_claude_skip_rule_any_reason(session):
    rules = review_auto_ignore.Rules(countries=(), claude_any=True)
    stack, old = opp(session), opp(session, company="Old")
    claude_says(session, stack, kind="stack", detail="")
    # an analysis from before ignore_reason
    claude_says(session, old, kind=None)
    assert review_auto_ignore.reason_for(session, stack, rules, NOW) == "Claude: stack"
    assert review_auto_ignore.reason_for(session, old, rules, NOW) == "Claude: skip"
    # Picked reasons alone never match an analysis without one.
    assert (
        review_auto_ignore.reason_for(
            session, old, review_auto_ignore.Rules(countries=(), claude_kinds=("other",)), NOW
        )
        is None
    )


def test_claude_skip_rule_ignores_apply_and_maybe_and_uses_the_latest(session):
    rules = review_auto_ignore.Rules(countries=(), claude_any=True)
    o = opp(session)
    claude_says(session, o, verdict="maybe", kind="none", detail="")
    assert review_auto_ignore.reason_for(session, o, rules, NOW) is None
    # skip
    claude_says(session, o)
    # analysed again
    claude_says(session, o, verdict="apply", kind="none", detail="")
    assert review_auto_ignore.reason_for(session, o, rules, NOW) is None


def test_claude_skip_rule_round_trip_and_apply_to_review(session):
    rules = review_auto_ignore.Rules(countries=(), claude_kinds=("location",))
    review_auto_ignore.save_rules(session, rules)
    assert review_auto_ignore.load_rules(session) == rules
    new, mine = opp(session), opp(session, company="B", state=models.OpportunityState.INTERESTED)
    claude_says(session, new)
    claude_says(session, mine)
    assert review_auto_ignore.apply_to_review(session, NOW) == 1
    assert (new.state, new.auto_ignore_reason) == (
        models.OpportunityState.AUTO_IGNORE,
        "Claude: location: EU only",
    )
    # your own state is never changed
    assert mine.state is models.OpportunityState.INTERESTED
