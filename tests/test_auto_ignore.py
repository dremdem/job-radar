from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from job_radar.auto_ignore import Rules, apply_to_review, load_rules, reason_for, save_rules
from job_radar.dedup import get_or_create_company
from job_radar.linkedin.alerts import AlertJob, add_linkedin_job
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    Opportunity,
    OpportunityState,
    SourceType,
)
from job_radar.rejections import mark_rejected

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ALL_ON = Rules(hybrid=True, applied=True, ignored=True, rejected=True, days=90)


def opp(
    session,
    company="Capitole",
    title="Senior Python Developer",
    location="Spain (Remote)",
    state=OpportunityState.NEW,
):
    o = Opportunity(
        title=title, company=get_or_create_company(session, company), location=location, state=state
    )
    session.add(o)
    session.commit()
    return o


def applied(session, company, at):
    session.add(
        Application(
            company_id=get_or_create_company(session, company).id,
            applied_at=at,
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()


def test_rules_are_off_by_default(session):
    rules = load_rules(session)
    assert not (rules.hybrid or rules.applied or rules.ignored or rules.rejected)
    assert rules.days == 90
    assert reason_for(session, opp(session, location="Madrid (Hybrid)"), rules, NOW) is None


def test_settings_round_trip(session):
    save_rules(session, ALL_ON)
    assert load_rules(session) == ALL_ON


def test_hybrid_rule(session):
    assert (
        reason_for(session, opp(session, location="Berlin (Hybrid)"), ALL_ON, NOW)
        == "hybrid / on-site"
    )
    assert (
        reason_for(
            session, opp(session, title="Backend Engineer (On-site)", location=None), ALL_ON, NOW
        )
        == "hybrid / on-site"
    )
    assert reason_for(session, opp(session, location="Germany (Remote)"), ALL_ON, NOW) is None


def test_applied_rule_respects_the_window(session):
    applied(session, "Capitole", NOW - timedelta(days=10))
    assert reason_for(session, opp(session), ALL_ON, NOW) == "applied to Capitole on 2026-09-17"
    applied(session, "Old Co", NOW - timedelta(days=200))
    assert reason_for(session, opp(session, company="Old Co"), ALL_ON, NOW) is None


def test_applied_rule_counts_vacancies_you_marked_applied(session):
    # Sii Poland: one role in three cities, two marked applied in Job Radar, no mail (#211).
    katowice = opp(session, company="Sii Poland", state=OpportunityState.APPLIED)
    katowice.state_changed_at = NOW - timedelta(days=3)
    session.commit()
    warsaw = opp(session, company="Sii Poland", location="Warsaw (Remote)")
    assert reason_for(session, warsaw, ALL_ON, NOW) == "applied to Sii Poland on 2026-09-24"
    katowice.state_changed_at = NOW - timedelta(days=200)
    session.commit()
    assert reason_for(session, warsaw, ALL_ON, NOW) is None
    # The newer of the two counts: a confirmed application in the mail still wins.
    applied(session, "Sii Poland", NOW - timedelta(days=1))
    assert reason_for(session, warsaw, ALL_ON, NOW) == "applied to Sii Poland on 2026-09-26"


def test_ignored_rule_counts_only_your_own_ignore(session):
    first = opp(session, state=OpportunityState.IGNORE)
    first.updated_at = NOW - timedelta(days=2)
    session.commit()
    assert (
        reason_for(session, opp(session), ALL_ON, NOW)
        == "ignored another Capitole vacancy on 2026-09-25"
    )
    auto = opp(session, company="Auto Co", state=OpportunityState.AUTO_IGNORE)
    assert reason_for(session, opp(session, company="Auto Co"), ALL_ON, NOW) is None
    assert auto.id


def test_rejected_rule_comes_first(session):
    company = get_or_create_company(session, "Capitole")
    mark_rejected(session, company_id=company.id, rejected_at=NOW - timedelta(days=5))
    applied(session, "Capitole", NOW - timedelta(days=8))
    session.commit()
    assert reason_for(session, opp(session), ALL_ON, NOW) == "Capitole rejected you on 2026-09-22"


def test_each_rule_can_be_switched_off(session):
    applied(session, "Capitole", NOW - timedelta(days=10))
    rules = Rules(hybrid=True, applied=False, ignored=True, rejected=True, days=90)
    assert reason_for(session, opp(session), rules, NOW) is None


def test_new_vacancies_are_auto_ignored_on_import(session):
    save_rules(session, ALL_ON)
    add_linkedin_job(
        session,
        AlertJob("1", "Python Developer", "Acme", "Berlin (Hybrid)", None, False),
        SourceType.LINKEDIN_ALERT,
    )
    add_linkedin_job(
        session,
        AlertJob("2", "Python Developer", "Acme", "Germany (Remote)", None, False),
        SourceType.LINKEDIN_ALERT,
    )
    session.commit()
    states = {
        o.location: (o.state, o.auto_ignore_reason) for o in session.scalars(select(Opportunity))
    }
    assert states["Berlin (Hybrid)"] == (OpportunityState.AUTO_IGNORE, "hybrid / on-site")
    assert states["Germany (Remote)"] == (OpportunityState.NEW, None)


def test_apply_to_review_touches_only_new(session):
    hybrid_new = opp(session, location="Berlin (Hybrid)")
    hybrid_liked = opp(session, location="Munich (Hybrid)", state=OpportunityState.INTERESTED)
    save_rules(session, ALL_ON)
    assert apply_to_review(session, now=NOW) == 1
    for o in (hybrid_new, hybrid_liked):
        session.refresh(o)
    assert hybrid_new.state is OpportunityState.AUTO_IGNORE
    assert hybrid_liked.state is OpportunityState.INTERESTED


def with_description(session, text, title="Backend Engineer"):
    from job_radar.dedup import add_snapshot, upsert_source_record

    o = opp(session, title=title)
    record = upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    add_snapshot(session, record, text)
    session.commit()
    return o


def test_no_python_rule(session):
    rules = Rules(no_python=True)
    assert reason_for(session, with_description(session, "Go and Rust only."), rules, NOW) == (
        "no Python in the description"
    )
    assert reason_for(session, with_description(session, "Python and FastAPI."), rules, NOW) is None
    assert reason_for(session, opp(session), rules, NOW) is None  # no description yet
    python_title = with_description(session, "Go only.", title="Python Developer")
    assert reason_for(session, python_title, rules, NOW) is None


def test_description_from_the_bookmarklet_triggers_the_rule(session):
    from job_radar.linkedin.capture import import_capture

    save_rules(session, Rules(no_python=True))
    card = {"job_id": "501", "text": "Backend Engineer\nAcme\nGermany (Remote)"}
    import_capture(session, {"cards": [card]})
    o = session.scalar(select(Opportunity))
    assert o.state is OpportunityState.NEW  # no description yet
    import_capture(session, {"cards": [], "detail": {"job_id": "501", "description": "We use Go."}})
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (
        OpportunityState.AUTO_IGNORE,
        "no Python in the description",
    )


def test_language_rule_reads_the_description(session):
    from job_radar.auto_ignore import Rules, reason_for
    from job_radar.dedup import add_snapshot, get_or_create_company, upsert_source_record
    from job_radar.models import Opportunity, SourceType

    o = Opportunity(title="Senior Python Developer", company=get_or_create_company(session, "Acme"))
    session.add(o)
    session.flush()
    record = upsert_source_record(
        session, source_type=SourceType.LINKEDIN_CAPTURE, external_id="1", url=None, opportunity=o
    )
    add_snapshot(
        session,
        record,
        "Wir suchen einen erfahrenen Entwickler für unser Team. Du arbeitest mit Python "
        "und Django an der Plattform, die von unseren Kunden jeden Tag genutzt wird.",
    )
    session.commit()
    session.refresh(o)
    assert reason_for(session, o, Rules()) is None
    assert reason_for(session, o, Rules(language=True)) == "language: German"
