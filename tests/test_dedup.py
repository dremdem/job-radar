"""Tests for job_radar.dedup: company name matching, idempotent upserts, snapshots and
merging two vacancies (docs/data-model.md).
"""

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc

import job_radar.dedup as dedup
import job_radar.models as models


def count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme", "acme"),
        ("Acme, Inc.", "acme"),
        ("ACME GmbH", "acme"),
        ("  Acme   Labs Ltd ", "acme labs"),
        ("Foo-Bar LLC", "foo bar"),
    ],
)
def test_normalize_company_name(raw, expected):
    assert dedup.normalize_company_name(raw) == expected


def test_get_or_create_company_is_idempotent(session):
    first = dedup.get_or_create_company(session, "Acme, Inc.")
    second = dedup.get_or_create_company(session, "ACME")
    assert first.id == second.id
    assert count(session, models.Company) == 1


def test_get_or_create_company_matches_alias(session):
    company = dedup.get_or_create_company(session, "Alphabet")
    company.aliases = ["google"]
    session.flush()
    assert dedup.get_or_create_company(session, "Google LLC").id == company.id


def make_opportunity(session) -> models.Opportunity:
    opportunity = models.Opportunity(
        title="Senior Python Engineer", company=dedup.get_or_create_company(session, "Acme")
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def test_upsert_source_record_is_idempotent(session):
    opportunity = make_opportunity(session)
    kwargs = dict(
        source_type=models.SourceType.LINKEDIN_ALERT,
        external_id="4012345678",
        url="https://www.linkedin.com/jobs/view/4012345678/",
        opportunity=opportunity,
    )
    first = dedup.upsert_source_record(session, **kwargs)
    second = dedup.upsert_source_record(session, **kwargs)
    assert first.id == second.id
    assert count(session, models.SourceRecord) == 1


def test_source_record_unique_constraint(session):
    opportunity = make_opportunity(session)
    for _ in range(2):
        session.add(
            models.SourceRecord(
                source_type=models.SourceType.LINKEDIN_ALERT,
                external_id="42",
                url="https://example.com/42",
                opportunity=opportunity,
            )
        )
    with pytest.raises(sa_exc.IntegrityError):
        session.flush()


def test_add_snapshot_dedups_by_content_hash(session):
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_ALERT,
        external_id="1",
        url="https://example.com/1",
        opportunity=make_opportunity(session),
    )
    _, created = dedup.add_snapshot(session, record, "Remote, worldwide. Python, FastAPI.")
    _, created_again = dedup.add_snapshot(session, record, "Remote, worldwide. Python, FastAPI.")
    _, created_changed = dedup.add_snapshot(session, record, "Remote, EU only. Python, FastAPI.")
    assert (created, created_again, created_changed) == (True, False, True)
    assert count(session, models.JobSnapshot) == 2
