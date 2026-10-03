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
    LINKEDIN_SEARCH = "linkedin_search"  # opt-in public guest search, see docs
    LINKEDIN_CAPTURE = "linkedin_capture"  # page sent by the user's bookmarklet
    LINKEDIN_LINK = "linkedin_link"  # a LinkedIn job seen as the original link elsewhere
    GMAIL = "gmail"
    HH = "hh"  # hh.ru vacancy, external_id = its id (docs/hh.md)
    HIMALAYAS = "himalayas"
    TELEGRAM = "telegram"


class OpportunityState(enum.StrEnum):
    NEW = "new"
    INTERESTED = "interested"
    IGNORE = "ignore"
    APPLIED = "applied"
    AUTO_IGNORE = "auto_ignore"  # set only by the rules in docs/settings.md


class EmailDirection(enum.StrEnum):
    IN = "in"
    OUT = "out"


class RejectionSource(enum.StrEnum):
    MANUAL = "manual"
    GMAIL = "gmail"


class ApplicationSource(enum.StrEnum):
    LINKEDIN = "linkedin"
    ATS = "ats"
    HH = "hh"  # inferred from an hh.ru rejection (docs/hh.md)
    MANUAL = "manual"


class ApplicationReview(enum.StrEnum):
    CONFIRMED = "confirmed"
    SUGGESTED = "suggested"  # found by heuristics, waits for the user
    DISMISSED = "dismissed"  # kept so re-imports do not suggest it again


class NoteKind(enum.StrEnum):
    ONLY_LOCATION = "only_location"
    LANGUAGE = "language"
    STACK = "stack"
    SENIORITY = "seniority"
    SALARY = "salary"
    CONTRACT = "contract"
    WORK_FORMAT = "work_format"  # hybrid / on-site (#117)
    OTHER = "other"
    NOTE = "note"  # a free note, not a reason to ignore


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="company")
    rejections: Mapped[list["Rejection"]] = relationship(back_populates="company")


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
    auto_ignore_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    company: Mapped[Company] = relationship(back_populates="opportunities")
    source_records: Mapped[list["SourceRecord"]] = relationship(back_populates="opportunity")
    analyses: Mapped[list["DescriptionAnalysis"]] = relationship(
        back_populates="opportunity", order_by="DescriptionAnalysis.id"
    )


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
    role: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[ApplicationSource] = mapped_column(
        _enum(ApplicationSource), default=ApplicationSource.MANUAL
    )
    review: Mapped[ApplicationReview] = mapped_column(
        _enum(ApplicationReview), default=ApplicationReview.CONFIRMED, index=True
    )
    email_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_messages.id"), unique=True
    )
    linkedin_job_id: Mapped[str | None] = mapped_column(String(32))

    company: Mapped[Company] = relationship()


class Rejection(Base):
    """A company said no. One rejection is enough to flag all of that company's
    opportunities; see docs/data-model.md. Recorded manually or detected from mail.
    """

    __tablename__ = "rejections"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    # One rejection per source email; NULLs (manual rejections) stay distinct in SQLite.
    email_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_messages.id"), unique=True
    )
    source: Mapped[RejectionSource] = mapped_column(_enum(RejectionSource))
    role: Mapped[str | None] = mapped_column(String(255))
    rejected_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    company: Mapped[Company] = relationship(back_populates="rejections")


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
    company_match: Mapped[str | None] = mapped_column(String(16))  # exact / probable


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_message_at: Mapped[datetime | None]
    last_run_at: Mapped[datetime | None]


class LinkedInFetch(Base):
    """Audit log of requests to LinkedIn's public guest search (opt-in, #23)."""

    __tablename__ = "linkedin_fetches"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    email_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_messages.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="search")  # search / description
    keywords: Mapped[str] = mapped_column(String(255), default="")
    geo_id: Mapped[str | None] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(String(32))
    start: Mapped[int]
    http_status: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(16))  # ok / blocked / error
    jobs: Mapped[int] = mapped_column(default=0)


class OpportunityNote(Base):
    """Why a vacancy was ignored, or a free note. See docs/applications.md#reasons-and-notes."""

    __tablename__ = "opportunity_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[NoteKind] = mapped_column(_enum(NoteKind))
    detail: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    company: Mapped[Company] = relationship()


class AppSetting(Base):
    """Settings edited on the Settings page, as JSON values. See docs/settings.md."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON)


class TelegramPost(Base):
    """Every post read from an allowlisted Telegram channel, once. See docs/telegram.md."""

    __tablename__ = "telegram_posts"
    __table_args__ = (UniqueConstraint("chat", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    chat: Mapped[str] = mapped_column(String(64))
    topic_id: Mapped[int | None]
    message_id: Mapped[int]
    posted_at: Mapped[datetime]
    text: Mapped[str] = mapped_column(Text, default="")
    forwarded_from: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))  # vacancy / skipped
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))


class DescriptionAnalysis(Base):
    """What a local model found in one description snapshot, quotes verified. See docs/ai.md."""

    __tablename__ = "description_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("job_snapshots.id"), unique=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    model: Mapped[str] = mapped_column(String(64))
    analyzed_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Only facts whose quote is literally in the description; the rest is "unknown" / empty.
    work_mode: Mapped[str] = mapped_column(String(16), default="unknown")
    work_mode_quote: Mapped[str] = mapped_column(Text, default="")
    residence: Mapped[list[str]] = mapped_column(JSON, default=list)
    residence_quote: Mapped[str] = mapped_column(Text, default="")
    # Unknown technologies: [{skill, status, quote}]; None = not checked yet (docs/ai.md).
    stack: Mapped[list[dict[str, str]] | None] = mapped_column(JSON, default=None)
    # The model's answers as is, to see what was dropped and why.
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    opportunity: Mapped["Opportunity"] = relationship(back_populates="analyses")


class ClaudeAnalysis(Base):
    """A deep analysis of one vacancy by Claude (claude -p). See docs/claude.md."""

    __tablename__ = "claude_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued / running / done / failed
    model: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float | None]  # as reported by Claude Code; notional on a subscription
    attempts: Mapped[int] = mapped_column(default=0)  # transient errors are retried (#113)

    opportunity: Mapped["Opportunity"] = relationship()


class ClaudeMessage(Base):
    """One message of the chat about a vacancy, continuing its analysis session (#106)."""

    __tablename__ = "claude_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("claude_analyses.id"))
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    text: Mapped[str] = mapped_column(Text, default="")
    # For a user message: queued / running / done / failed (with error).
    state: Mapped[str] = mapped_column(String(16), default="done", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    analysis: Mapped[ClaudeAnalysis] = relationship()
