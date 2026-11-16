"""Idempotent upserts, so re-running an import never creates duplicates.

Companies are matched by a normalized name (case, legal suffixes and punctuation
ignored) and by aliases. A source record is unique per source type and external id, and
a description snapshot is stored only when its content hash is new.
``merge_opportunities`` moves everything of one vacancy to another when two turn out to
be the same. See docs/data-model.md.

Usage:
    import job_radar.dedup as dedup
"""

import hashlib
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "gmbh", "ag", "bv", "sa", "sarl",
    "plc", "corp", "corporation", "co", "oy", "ab", "as", "srl", "spa", "pty",
}  # fmt: skip


def normalize_company_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def get_or_create_company(session: sa_orm.Session, name: str) -> models.Company:
    normalized = normalize_company_name(name)
    company = session.scalar(
        sa.select(models.Company).where(models.Company.normalized_name == normalized)
    )
    if company is None:
        # Aliases are a JSON list; the table is small, so scan in Python.
        company = next(
            (
                c
                for c in session.scalars(sa.select(models.Company))
                if normalized in (c.aliases or [])
            ),
            None,
        )
    if company is None:
        company = models.Company(
            name=name.strip(), normalized_name=normalized, aliases=[], domains=[]
        )
        session.add(company)
        session.flush()
    return company


def upsert_source_record(
    session: sa_orm.Session,
    *,
    source_type: models.SourceType,
    external_id: str,
    url: str | None,
    opportunity: models.Opportunity,
) -> models.SourceRecord:
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type == source_type,
            models.SourceRecord.external_id == external_id,
        )
    )
    if record is None:
        record = models.SourceRecord(
            source_type=source_type, external_id=external_id, url=url, opportunity=opportunity
        )
        session.add(record)
        session.flush()
    return record


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def add_snapshot(
    session: sa_orm.Session, source_record: models.SourceRecord, raw_text: str
) -> tuple[models.JobSnapshot, bool]:
    """Store raw_text unless an identical snapshot exists. Returns (snapshot, created)."""
    digest = content_hash(raw_text)
    snapshot = session.scalar(
        sa.select(models.JobSnapshot).where(
            models.JobSnapshot.source_record_id == source_record.id,
            models.JobSnapshot.content_hash == digest,
        )
    )
    if snapshot is not None:
        return snapshot, False
    snapshot = models.JobSnapshot(
        source_record=source_record, raw_text=raw_text, content_hash=digest
    )
    session.add(snapshot)
    session.flush()
    return snapshot, True


def merge_opportunities(
    session: sa_orm.Session, keep: models.Opportunity, dup: models.Opportunity
) -> None:
    """Move everything of ``dup`` (sources, mail, notes, analyses, chat, events) to ``keep``
    and delete ``dup``. Source records move through the ORM, so a caller holding one sees
    ``record.opportunity`` become ``keep`` (#172, #269)."""
    import job_radar.models as models

    for record in list(dup.source_records):
        record.opportunity = keep
    session.flush()
    for model in (
        models.Rejection,
        models.Application,
        models.OpportunityNote,
        models.TelegramPost,
        models.DescriptionAnalysis,
        models.ClaudeAnalysis,
        models.ClaudeMessage,
        models.CompanyEvent,
    ):
        session.execute(
            sa.update(model).where(model.opportunity_id == dup.id).values(opportunity_id=keep.id)
        )
    session.flush()
    session.expire(keep)
    session.expire(dup)
    session.delete(dup)
    session.flush()
