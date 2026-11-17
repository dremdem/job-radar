"""A vacancy you applied to never comes back to the queue (#269)."""

import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.sources.linkedin.alerts as linkedin_alerts

S = models.OpportunityState


def applied(session, title="Senior Python Engineer", company="Acme", state=S.APPLIED):
    o = models.Opportunity(
        title=title, company=dedup.get_or_create_company(session, company), state=state
    )
    session.add(o)
    session.flush()
    hh_id = str(9000 + o.id)
    url = f"https://hh.ru/vacancy/{hh_id}"
    session.add(
        models.SourceRecord(
            source_type=models.SourceType.HH, external_id=hh_id, url=url, opportunity=o
        )
    )
    session.commit()
    return o


def alert(session, job_id, title="Senior Python Engineer", company="Acme"):
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob(job_id, title, company, "Remote", None, None),
        models.SourceType.LINKEDIN_ALERT,
    )
    session.commit()


def in_queue(session):
    return session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.state == S.NEW)
    ).all()


def test_same_vacancy_from_another_source_joins_the_applied_one(session):
    original = applied(session)
    # case and punctuation differ
    alert(session, "4471", title="senior python engineer!")
    assert in_queue(session) == []
    assert session.scalar(sa.select(sa.func.count(models.Opportunity.id))) == 1
    session.refresh(original)
    assert original.state is S.APPLIED
    kinds = {(r.source_type, r.external_id) for r in original.source_records}
    assert (models.SourceType.LINKEDIN_ALERT, "4471") in kinds
    event = session.scalar(
        sa.select(models.CompanyEvent).where(models.CompanyEvent.opportunity_id == original.id)
    )
    assert event.kind == "seen again" and "linkedin_alert" in event.comment
    assert "linkedin.com/jobs/view/4471" in event.comment


def test_seen_again_later_with_a_description_still_joins(session):
    """The hook is in the rules every import runs, so a description source joins too."""
    original = applied(session)
    o = models.Opportunity(title="Senior Python Engineer", company=original.company, state=S.NEW)
    session.add(o)
    session.flush()
    record = models.SourceRecord(source_type=models.SourceType.HIMALAYAS, external_id="h1", url="https://x/1",
                          opportunity=o)  # fmt: skip
    session.add(record)
    session.flush()
    dedup.add_snapshot(session, record, "Python, remote.")
    import job_radar.review.auto_ignore as review_auto_ignore

    assert review_auto_ignore.apply_rules(session, o) is True
    session.commit()
    # callers that hold the record see the original
    assert record.opportunity.id == original.id
    assert in_queue(session) == []


def test_another_vacancy_of_the_company_is_left_alone(session):
    applied(session)
    alert(session, "4472", title="Data Engineer")
    assert [o.title for o in session.scalars(sa.select(models.Opportunity))
            if o.state is not S.APPLIED] == ["Data Engineer"]  # fmt: skip


def test_two_applied_with_that_title_is_ambiguous_and_left_alone(session):
    applied(session)
    # another city or team on hh.ru
    applied(session)
    alert(session, "4473")
    assert session.scalar(sa.select(sa.func.count(models.Opportunity.id))) == 3


def test_not_applied_original_is_not_a_reason(session):
    applied(session, state=S.IGNORE)
    alert(session, "4474")
    assert session.scalar(sa.select(sa.func.count(models.Opportunity.id))) == 2


def test_merge_command_lists_then_merges_existing_copies(session):
    import job_radar.review.applied_duplicates as review_applied_duplicates

    original = applied(session)
    copies = [applied(session, title="Senior  Python Engineer", state=st)
              for st in (S.AUTO_IGNORE, S.IGNORE, S.NEW)]  # fmt: skip
    session.add(models.OpportunityNote(opportunity_id=copies[1].id, company_id=original.company_id,
                                kind=models.NoteKind.STACK, detail="Go"))  # fmt: skip
    # yours: stays
    kept = applied(session, title="Senior Python Engineer!", state=S.INTERESTED)
    ambiguous = [applied(session, title="Backend Dev", company="Beta") for _ in range(2)]
    beta_copy = applied(session, title="backend dev", company="Beta", state=S.IGNORE)
    session.commit()

    plan = review_applied_duplicates.merge_applied_duplicates(session, apply=False)
    assert plan == [(original.id, sorted(c.id for c in copies))]
    # a dry run changes nothing
    assert session.scalar(sa.select(sa.func.count(models.Opportunity.id))) == 8

    assert review_applied_duplicates.merge_applied_duplicates(session, apply=True) == plan
    session.expire_all()
    ids = set(session.scalars(sa.select(models.Opportunity.id)))
    assert ids == {original.id, kept.id, *(a.id for a in ambiguous), beta_copy.id}
    assert len(session.get(models.Opportunity, original.id).source_records) == 4
    assert session.scalar(sa.select(models.OpportunityNote.opportunity_id)) == original.id
    assert review_applied_duplicates.merge_applied_duplicates(session, apply=False) == []


def test_skipped_groups_stay_apart(session):
    import job_radar.review.applied_duplicates as review_applied_duplicates

    a = applied(session)
    a_copy = applied(session, state=S.AUTO_IGNORE)
    b = applied(session, title="Software Engineer")
    b_copy = applied(session, title="software engineer", state=S.AUTO_IGNORE)
    assert review_applied_duplicates.merge_applied_duplicates(
        session, apply=True, skip=frozenset({b.id})
    ) == [(a.id, [a_copy.id])]
    session.expire_all()
    assert session.get(models.Opportunity, a_copy.id) is None
    # left for you to look at
    assert session.get(models.Opportunity, b_copy.id) is not None
