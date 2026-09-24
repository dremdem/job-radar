import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from job_radar.dedup import (
    add_snapshot,
    get_or_create_company,
    normalize_company_name,
    upsert_source_record,
)
from job_radar.models import Company, JobSnapshot, Opportunity, SourceRecord, SourceType


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


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
    assert normalize_company_name(raw) == expected


def test_get_or_create_company_is_idempotent(session):
    first = get_or_create_company(session, "Acme, Inc.")
    second = get_or_create_company(session, "ACME")
    assert first.id == second.id
    assert count(session, Company) == 1


def test_get_or_create_company_matches_alias(session):
    company = get_or_create_company(session, "Alphabet")
    company.aliases = ["google"]
    session.flush()
    assert get_or_create_company(session, "Google LLC").id == company.id


def make_opportunity(session) -> Opportunity:
    opportunity = Opportunity(
        title="Senior Python Engineer", company=get_or_create_company(session, "Acme")
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def test_upsert_source_record_is_idempotent(session):
    opportunity = make_opportunity(session)
    kwargs = dict(
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="4012345678",
        url="https://www.linkedin.com/jobs/view/4012345678/",
        opportunity=opportunity,
    )
    first = upsert_source_record(session, **kwargs)
    second = upsert_source_record(session, **kwargs)
    assert first.id == second.id
    assert count(session, SourceRecord) == 1


def test_source_record_unique_constraint(session):
    opportunity = make_opportunity(session)
    for _ in range(2):
        session.add(
            SourceRecord(
                source_type=SourceType.LINKEDIN_ALERT,
                external_id="42",
                url="https://example.com/42",
                opportunity=opportunity,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_add_snapshot_dedups_by_content_hash(session):
    record = upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="1",
        url="https://example.com/1",
        opportunity=make_opportunity(session),
    )
    _, created = add_snapshot(session, record, "Remote, worldwide. Python, FastAPI.")
    _, created_again = add_snapshot(session, record, "Remote, worldwide. Python, FastAPI.")
    _, created_changed = add_snapshot(session, record, "Remote, EU only. Python, FastAPI.")
    assert (created, created_again, created_changed) == (True, False, True)
    assert count(session, JobSnapshot) == 2
