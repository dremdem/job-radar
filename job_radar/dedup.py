"""Idempotent upserts, so re-running an import never creates duplicates."""

import hashlib
import re

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Company, JobSnapshot, Opportunity, SourceRecord, SourceType

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "gmbh", "ag", "bv", "sa", "sarl",
    "plc", "corp", "corporation", "co", "oy", "ab", "as", "srl", "spa", "pty",
}  # fmt: skip


def normalize_company_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def get_or_create_company(session: Session, name: str) -> Company:
    normalized = normalize_company_name(name)
    company = session.scalar(select(Company).where(Company.normalized_name == normalized))
    if company is None:
        # Aliases are a JSON list; the table is small, so scan in Python.
        company = next(
            (c for c in session.scalars(select(Company)) if normalized in (c.aliases or [])),
            None,
        )
    if company is None:
        company = Company(name=name.strip(), normalized_name=normalized, aliases=[], domains=[])
        session.add(company)
        session.flush()
    return company


def upsert_source_record(
    session: Session,
    *,
    source_type: SourceType,
    external_id: str,
    url: str | None,
    opportunity: Opportunity,
) -> SourceRecord:
    record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type == source_type, SourceRecord.external_id == external_id
        )
    )
    if record is None:
        record = SourceRecord(
            source_type=source_type, external_id=external_id, url=url, opportunity=opportunity
        )
        session.add(record)
        session.flush()
    return record


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def add_snapshot(
    session: Session, source_record: SourceRecord, raw_text: str
) -> tuple[JobSnapshot, bool]:
    """Store raw_text unless an identical snapshot exists. Returns (snapshot, created)."""
    digest = content_hash(raw_text)
    snapshot = session.scalar(
        select(JobSnapshot).where(
            JobSnapshot.source_record_id == source_record.id, JobSnapshot.content_hash == digest
        )
    )
    if snapshot is not None:
        return snapshot, False
    snapshot = JobSnapshot(source_record=source_record, raw_text=raw_text, content_hash=digest)
    session.add(snapshot)
    session.flush()
    return snapshot, True


def merge_opportunities(session: Session, keep: Opportunity, dup: Opportunity) -> None:
    """Move everything of ``dup`` (sources, mail, notes, analyses, chat, events) to ``keep``
    and delete ``dup``. Source records move through the ORM, so a caller holding one sees
    ``record.opportunity`` become ``keep`` (#172, #269)."""
    from .models import (
        Application,
        ClaudeAnalysis,
        ClaudeMessage,
        CompanyEvent,
        DescriptionAnalysis,
        OpportunityNote,
        Rejection,
        TelegramPost,
    )

    for record in list(dup.source_records):
        record.opportunity = keep
    session.flush()
    for model in (
        Rejection,
        Application,
        OpportunityNote,
        TelegramPost,
        DescriptionAnalysis,
        ClaudeAnalysis,
        ClaudeMessage,
        CompanyEvent,
    ):
        session.execute(
            update(model).where(model.opportunity_id == dup.id).values(opportunity_id=keep.id)
        )
    session.flush()
    session.expire(keep)
    session.expire(dup)
    session.delete(dup)
    session.flush()
