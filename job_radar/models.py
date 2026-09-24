"""SQLAlchemy models. See docs/data-model.md."""

import enum
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


def _enum(cls: type[enum.Enum]) -> Enum:
    # Store values ("linkedin_alert"), not member names, and keep it a plain string column.
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=32)


class SourceType(enum.StrEnum):
    LINKEDIN_ALERT = "linkedin_alert"
    GMAIL = "gmail"
    HIMALAYAS = "himalayas"
    TELEGRAM = "telegram"


class OpportunityState(enum.StrEnum):
    NEW = "new"
    INTERESTED = "interested"
    IGNORE = "ignore"
    APPLIED = "applied"


class EmailDirection(enum.StrEnum):
    IN = "in"
    OUT = "out"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="company")


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    state: Mapped[OpportunityState] = mapped_column(
        _enum(OpportunityState), default=OpportunityState.NEW, index=True
    )
    score: Mapped[float | None]
    score_reasons: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    location: Mapped[str | None] = mapped_column(String(255))
    salary: Mapped[str | None] = mapped_column(String(64))
    easy_apply: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    company: Mapped[Company] = relationship(back_populates="opportunities")
    source_records: Mapped[list["SourceRecord"]] = relationship(back_populates="opportunity")


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("source_type", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType))
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    opportunity: Mapped[Opportunity] = relationship(back_populates="source_records")
    snapshots: Mapped[list["JobSnapshot"]] = relationship(back_populates="source_record")


class JobSnapshot(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (UniqueConstraint("source_record_id", "content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)

    source_record: Mapped[SourceRecord] = relationship(back_populates="snapshots")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    applied_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(32), default="applied")
    notes: Mapped[str | None] = mapped_column(Text)


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[EmailDirection] = mapped_column(_enum(EmailDirection))
    sent_at: Mapped[datetime]
    from_addr: Mapped[str] = mapped_column(String(320))
    to_addr: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    label_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Full bodies only for generated mailings (LinkedIn alerts), see docs/gmail.md.
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    detected_event: Mapped[str | None] = mapped_column(String(32))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_message_at: Mapped[datetime | None]
    last_run_at: Mapped[datetime | None]
