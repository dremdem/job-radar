"""SQLAlchemy models: the whole database schema in one place.

Core tables: ``Company`` and its ``Opportunity`` (vacancy) rows; every place a vacancy was
seen is a ``SourceRecord`` with ``JobSnapshot`` texts. Mail is stored as ``EmailMessage``;
``Application`` and ``Rejection`` come from it or are added by hand.
``OpportunityNote`` and ``CompanyEvent`` hold the user's own notes and events;
``UserAction`` logs each decision in the queue so the last one can be undone.
Analyses: ``DescriptionAnalysis`` (local model), ``ClaudeAnalysis`` with its chat in
``ClaudeMessage``, and Claude's memory in ``ClaudeMemory``. ``SyncCursor``,
``LinkedInFetch``, ``AppSetting`` and ``TelegramPost`` are bookkeeping; ``AtsBoard`` and
``AtsLookup`` remember employer boards and the search for original postings (#2). Enums are stored
as plain strings. Schema changes go through Alembic migrations. See docs/data-model.md.

Usage:
    import job_radar.models as models
"""

import datetime as dt
import enum
import typing as t

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(sa_orm.DeclarativeBase):
    """Declarative base of all models; maps ``datetime`` to timezone-aware columns."""

    # float stays FLOAT as in the migrations: SQLAlchemy 2.1 maps it to Double by default.
    type_annotation_map: t.ClassVar[dict[t.Any, t.Any]] = {
        dt.datetime: sa.DateTime(timezone=True),
        float: sa.Float(),
    }


def _enum(cls: type[enum.Enum]) -> sa.Enum:
    # Store values ("linkedin_alert"), not member names, and keep it a plain string column.
    return sa.Enum(
        cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=32
    )


class SourceType(enum.StrEnum):
    """Where a vacancy was seen; stored on ``SourceRecord.source_type``."""

    LINKEDIN_ALERT = "linkedin_alert"
    # opt-in public guest search, see docs
    LINKEDIN_SEARCH = "linkedin_search"
    # page sent by the user's bookmarklet
    LINKEDIN_CAPTURE = "linkedin_capture"
    # a LinkedIn job seen as the original link elsewhere
    LINKEDIN_LINK = "linkedin_link"
    GMAIL = "gmail"
    # hh.ru vacancy, external_id = its id (docs/hh.md)
    HH = "hh"
    HIMALAYAS = "himalayas"
    # small boards, docs/boards.md; external_id = the board's job id
    REMOTIVE = "remotive"
    WORKINGNOMADS = "workingnomads"
    # external_id = the job page URL
    WEWORKREMOTELY = "weworkremotely"
    # Jobicy API, external_id = its job id
    JOBICY = "jobicy"
    # Remote OK feed, external_id = its job id
    REMOTEOK = "remoteok"
    # FDE Times job list, external_id = the role slug
    FDETIMES = "fdetimes"
    # "Ask HN: Who is hiring?" post, external_id = comment id
    HACKERNEWS = "hackernews"
    TELEGRAM = "telegram"
    # added by hand; external_id = the link, or "manual:<n>" (#182)
    MANUAL = "manual"
    # the original posting on the employer's ATS board (#2, docs/ats.md);
    # external_id = "<board slug>:<job id>", job ids are unique only within a board
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"


class OpportunityState(enum.StrEnum):
    """Where a vacancy is in the queue: To review, Interested, Ignore, Applied, Auto-ignore."""

    NEW = "new"
    INTERESTED = "interested"
    IGNORE = "ignore"
    APPLIED = "applied"
    # set only by the rules in docs/settings.md
    AUTO_IGNORE = "auto_ignore"


class EmailDirection(enum.StrEnum):
    """Whether a stored email was received or sent by the user."""

    IN = "in"
    OUT = "out"


class RejectionSource(enum.StrEnum):
    """How a rejection was found: in mail or set by hand."""

    MANUAL = "manual"
    GMAIL = "gmail"


class ApplicationSource(enum.StrEnum):
    """How an application was found: LinkedIn, an ATS, hh.ru or by hand."""

    LINKEDIN = "linkedin"
    ATS = "ats"
    # inferred from an hh.ru rejection (docs/hh.md)
    HH = "hh"
    # FDE Times "Application received" mail (docs/fdetimes.md)
    FDETIMES = "fdetimes"
    MANUAL = "manual"


class ApplicationReview(enum.StrEnum):
    """Whether a detected application is confirmed, waiting for the user or dismissed."""

    CONFIRMED = "confirmed"
    # found by heuristics, waits for the user
    SUGGESTED = "suggested"
    # kept so re-imports do not suggest it again
    DISMISSED = "dismissed"


class NoteKind(enum.StrEnum):
    """The kind of a reason to ignore a vacancy, or a free note."""

    ONLY_LOCATION = "only_location"
    LANGUAGE = "language"
    STACK = "stack"
    SENIORITY = "seniority"
    SALARY = "salary"
    CONTRACT = "contract"
    # hybrid / on-site (#117)
    WORK_FORMAT = "work_format"
    OTHER = "other"
    # a free note, not a reason to ignore
    NOTE = "note"


class Company(Base):
    """An employer; vacancies, mail, applications and rejections hang off it."""

    __tablename__ = "companies"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    name: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255))
    normalized_name: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255), unique=True)
    aliases: sa_orm.Mapped[list[str]] = sa_orm.mapped_column(sa.JSON, default=list)
    domains: sa_orm.Mapped[list[str]] = sa_orm.mapped_column(sa.JSON, default=list)
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    opportunities: sa_orm.Mapped[list["Opportunity"]] = sa_orm.relationship(
        back_populates="company"
    )
    rejections: sa_orm.Mapped[list["Rejection"]] = sa_orm.relationship(back_populates="company")


class Opportunity(Base):
    """One vacancy in the queue, with its state, score and the data shown in the UI."""

    __tablename__ = "opportunities"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    title: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255))
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    state: sa_orm.Mapped[OpportunityState] = sa_orm.mapped_column(
        _enum(OpportunityState), default=OpportunityState.NEW, index=True
    )
    score: sa_orm.Mapped[float | None]
    score_reasons: sa_orm.Mapped[list[dict[str, str]]] = sa_orm.mapped_column(sa.JSON, default=list)
    location: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    salary: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(64))
    easy_apply: sa_orm.Mapped[bool | None]
    auto_ignore_reason: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    # the site says it is not available to you (#276)
    unavailable_at: sa_orm.Mapped[dt.datetime | None]
    # LinkedIn: not accepting applications (#278)
    not_accepting_at: sa_orm.Mapped[dt.datetime | None]
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)
    updated_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow, onupdate=utcnow)
    # your last state change, for sorting (#199)
    state_changed_at: sa_orm.Mapped[dt.datetime | None]

    company: sa_orm.Mapped[Company] = sa_orm.relationship(back_populates="opportunities")
    source_records: sa_orm.Mapped[list["SourceRecord"]] = sa_orm.relationship(
        back_populates="opportunity"
    )
    analyses: sa_orm.Mapped[list["DescriptionAnalysis"]] = sa_orm.relationship(
        back_populates="opportunity", order_by="DescriptionAnalysis.id"
    )


class SourceRecord(Base):
    """One place a vacancy was seen: an alert email, a board, a pasted link.

    Unique per source type and external id.
    """

    __tablename__ = "source_records"
    __table_args__ = (sa.UniqueConstraint("source_type", "external_id"),)

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    source_type: sa_orm.Mapped[SourceType] = sa_orm.mapped_column(_enum(SourceType))
    external_id: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255))
    url: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    first_seen_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    opportunity: sa_orm.Mapped[Opportunity] = sa_orm.relationship(back_populates="source_records")
    snapshots: sa_orm.Mapped[list["JobSnapshot"]] = sa_orm.relationship(
        back_populates="source_record"
    )


class JobSnapshot(Base):
    """A description text of a source record; a new one is stored only when the text changes."""

    __tablename__ = "job_snapshots"
    __table_args__ = (sa.UniqueConstraint("source_record_id", "content_hash"),)

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    source_record_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("source_records.id"), index=True
    )
    raw_text: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text)
    content_hash: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64))
    retrieved_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    source_record: sa_orm.Mapped[SourceRecord] = sa_orm.relationship(back_populates="snapshots")


class Application(Base):
    """The user applied to a company, from confirmation mail or added by hand."""

    __tablename__ = "applications"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    opportunity_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id")
    )
    applied_at: sa_orm.Mapped[dt.datetime]
    status: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(32), default="applied")
    notes: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    role: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    source: sa_orm.Mapped[ApplicationSource] = sa_orm.mapped_column(
        _enum(ApplicationSource), default=ApplicationSource.MANUAL
    )
    review: sa_orm.Mapped[ApplicationReview] = sa_orm.mapped_column(
        _enum(ApplicationReview), default=ApplicationReview.CONFIRMED, index=True
    )
    email_message_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("email_messages.id"), unique=True
    )
    linkedin_job_id: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(32))

    company: sa_orm.Mapped[Company] = sa_orm.relationship()


class Rejection(Base):
    """A company said no. One rejection is enough to flag all of that company's
    opportunities; see docs/data-model.md. Recorded manually or detected from mail.
    """

    __tablename__ = "rejections"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    opportunity_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id")
    )
    # One rejection per source email; NULLs (manual rejections) stay distinct in SQLite.
    email_message_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("email_messages.id"), unique=True
    )
    source: sa_orm.Mapped[RejectionSource] = sa_orm.mapped_column(_enum(RejectionSource))
    role: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    rejected_at: sa_orm.Mapped[dt.datetime]
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    company: sa_orm.Mapped[Company] = sa_orm.relationship(back_populates="rejections")


class EmailMessage(Base):
    """One Gmail message read by the app (metadata and text), linked to a company when known."""

    __tablename__ = "email_messages"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    gmail_message_id: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), unique=True)
    gmail_thread_id: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), index=True)
    direction: sa_orm.Mapped[EmailDirection] = sa_orm.mapped_column(_enum(EmailDirection))
    sent_at: sa_orm.Mapped[dt.datetime]
    from_addr: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(320))
    to_addr: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text)
    subject: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    snippet: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    label_ids: sa_orm.Mapped[list[str]] = sa_orm.mapped_column(sa.JSON, default=list)
    categories: sa_orm.Mapped[list[str]] = sa_orm.mapped_column(sa.JSON, default=list)
    # Full bodies only for generated mailings (LinkedIn alerts), see docs/gmail.md.
    body_html: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    body_text: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    detected_event: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(32))
    company_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("companies.id"), index=True
    )
    # exact / probable
    company_match: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(16))
    # Deleted from the company history (#225): kept so a sync does not bring it back.
    hidden_at: sa_orm.Mapped[dt.datetime | None]


class SyncCursor(Base):
    """Where an incremental import stopped, so the next run reads only new items."""

    __tablename__ = "sync_cursors"

    name: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), primary_key=True)
    last_message_at: sa_orm.Mapped[dt.datetime | None]
    last_run_at: sa_orm.Mapped[dt.datetime | None]


class LinkedInFetch(Base):
    """Audit log of requests to LinkedIn's public guest search (opt-in, #23)."""

    __tablename__ = "linkedin_fetches"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow, index=True)
    email_message_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("email_messages.id"), index=True
    )
    # search / description
    kind: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16), default="search")
    keywords: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255), default="")
    geo_id: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(32))
    job_id: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(32))
    start: sa_orm.Mapped[int]
    http_status: sa_orm.Mapped[int | None]
    # ok / blocked / error / stopped
    status: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    jobs: sa_orm.Mapped[int] = sa_orm.mapped_column(default=0)
    # Why it was not ok: "HTTP 429", "redirected to login", "stopped by you"... (#205)
    detail: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    # The vacancy a description was fetched for; no foreign key, so merging vacancies
    # never trips over the log.
    opportunity_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(index=True)


class OpportunityNote(Base):
    """Why a vacancy was ignored, or a free note. See docs/applications.md#reasons-and-notes."""

    __tablename__ = "opportunity_notes"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    kind: sa_orm.Mapped[NoteKind] = sa_orm.mapped_column(_enum(NoteKind))
    detail: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    comment: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    company: sa_orm.Mapped[Company] = sa_orm.relationship()


class UserAction(Base):
    """One decision you made in the queue (interested, ignore, applied, an ignore reason),
    with what the vacancy looked like before, so it can be undone with Z. See
    docs/queue.md#undo."""

    __tablename__ = "user_actions"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    # No foreign keys, as in LinkedInFetch: merging or deleting vacancies never trips over
    # the log; an action whose vacancy is gone is skipped.
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(index=True)
    from_state: sa_orm.Mapped[OpportunityState] = sa_orm.mapped_column(_enum(OpportunityState))
    to_state: sa_orm.Mapped[OpportunityState] = sa_orm.mapped_column(_enum(OpportunityState))
    from_state_changed_at: sa_orm.Mapped[dt.datetime | None]
    from_auto_ignore_reason: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    # the reason an ignore with a reason added; undo deletes it
    note_id: sa_orm.Mapped[int | None]
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)
    undone_at: sa_orm.Mapped[dt.datetime | None]


# The mark the Chrome fill leaves in a vacancy's history (#272). Not a contact, not your action.
# A vacancy the site says is not available is Opportunity.unavailable_at instead (#276).
FILL_PROBLEM = "fill_problem"
FILL_EVENT_KINDS = frozenset({FILL_PROBLEM})


class CompanyEvent(Base):
    """Something that happened with a company and is not in the mail: a call, an interview,
    a recruiter on Telegram. Added by hand (#182)."""

    __tablename__ = "company_events"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    opportunity_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    # see manual.EVENT_KINDS
    kind: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(32))
    at: sa_orm.Mapped[dt.datetime]
    comment: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)


class AppSetting(Base):
    """Settings edited on the Settings page, as JSON values. See docs/settings.md."""

    __tablename__ = "app_settings"

    key: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), primary_key=True)
    value: sa_orm.Mapped[dict | list | str | int | bool | None] = sa_orm.mapped_column(sa.JSON)


class TelegramPost(Base):
    """Every post read from an allowlisted Telegram channel, once. See docs/telegram.md."""

    __tablename__ = "telegram_posts"
    __table_args__ = (sa.UniqueConstraint("chat", "message_id"),)

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    source: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), index=True)
    chat: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64))
    topic_id: sa_orm.Mapped[int | None]
    message_id: sa_orm.Mapped[int]
    posted_at: sa_orm.Mapped[dt.datetime]
    text: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    forwarded_from: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    # vacancy / skipped
    status: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    opportunity_id: sa_orm.Mapped[int | None] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id")
    )


class AtsBoard(Base):
    """An employer's job board on an ATS (Greenhouse, Lever, Ashby, Workable), see docs/ats.md.

    Kept apart from the company's aliases: ``status`` says how sure the link is. Only a
    ``verified`` board gets its postings attached; a ``guessed`` one (a slug derived from
    the name that the API could not tie to the company) gives candidates only; ``absent``
    remembers a guess the API did not know, so it is not asked again for a month.
    """

    __tablename__ = "ats_boards"
    __table_args__ = (sa.UniqueConstraint("provider", "slug"),)

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    company_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("companies.id"), index=True)
    # greenhouse / lever / ashby / workable
    provider: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    slug: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(128))
    # verified / guessed / absent
    status: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    # link (a tenant URL in stored data) / name (the board's own company name) / manual / guess
    found_by: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    # the company name the board gives, when the API has one
    board_name: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    checked_at: sa_orm.Mapped[dt.datetime | None]
    error: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)

    company: sa_orm.Mapped[Company] = sa_orm.relationship()


class AtsLookup(Base):
    """The last search for a vacancy's original posting, one row per vacancy (docs/ats.md).

    ``candidates`` lists the postings that could be it when the match is not certain, for
    the user to pick by hand.
    """

    __tablename__ = "ats_lookups"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    # No foreign key, as in UserAction: merging vacancies never trips over the log.
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(unique=True)
    # matched / ambiguous / no_match / no_board / taken / error
    status: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    board_id: sa_orm.Mapped[int | None]
    # [{provider, slug, id, title, location, url}]
    candidates: sa_orm.Mapped[list[dict[str, str]]] = sa_orm.mapped_column(sa.JSON, default=list)
    detail: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(255))
    checked_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(default=utcnow)


class DescriptionAnalysis(Base):
    """What a local model found in one description snapshot, quotes verified. See docs/ai.md."""

    __tablename__ = "description_analyses"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    snapshot_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("job_snapshots.id"), unique=True
    )
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    model: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64))
    analyzed_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )
    # Only facts whose quote is literally in the description; the rest is "unknown" / empty.
    work_mode: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16), default="unknown")
    work_mode_quote: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    residence: sa_orm.Mapped[list[str]] = sa_orm.mapped_column(sa.JSON, default=list)
    residence_quote: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    # Unknown technologies: [{skill, status, quote}]; None = not checked yet (docs/ai.md).
    stack: sa_orm.Mapped[list[dict[str, str]] | None] = sa_orm.mapped_column(sa.JSON, default=None)
    # The model's answers as is, to see what was dropped and why.
    raw: sa_orm.Mapped[dict[str, t.Any]] = sa_orm.mapped_column(sa.JSON, default=dict)

    opportunity: sa_orm.Mapped["Opportunity"] = sa_orm.relationship(back_populates="analyses")


class ClaudeAnalysis(Base):
    """A deep analysis of one vacancy by Claude (claude -p). See docs/claude.md."""

    __tablename__ = "claude_analyses"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    state: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16), default="queued", index=True)
    # queued / running / done / failed
    model: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(32))
    prompt_version: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    session_id: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.String(64))
    requested_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )
    started_at: sa_orm.Mapped[dt.datetime | None]
    finished_at: sa_orm.Mapped[dt.datetime | None]
    result: sa_orm.Mapped[dict[str, t.Any] | None] = sa_orm.mapped_column(sa.JSON)
    error: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    # as reported by Claude Code; notional on a subscription
    cost_usd: sa_orm.Mapped[float | None]
    # transient errors are retried (#113)
    attempts: sa_orm.Mapped[int] = sa_orm.mapped_column(default=0)

    opportunity: sa_orm.Mapped["Opportunity"] = sa_orm.relationship()


class ClaudeMessage(Base):
    """One message of the chat about a vacancy, continuing its analysis session (#106)."""

    __tablename__ = "claude_messages"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    opportunity_id: sa_orm.Mapped[int] = sa_orm.mapped_column(
        sa.ForeignKey("opportunities.id"), index=True
    )
    analysis_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("claude_analyses.id"))
    # user / assistant
    role: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    text: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    # For a user message: queued / running / done / failed (with error).
    state: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16), default="done", index=True)
    error: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    created_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )
    # what this answer changed (#215)
    memory_note: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)

    analysis: sa_orm.Mapped[ClaudeAnalysis] = sa_orm.relationship()


class ClaudeMemory(Base):
    """A fact or a skill Claude keeps between chats. See docs/claude-memory.md."""

    __tablename__ = "claude_memory"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    # fact / skill
    kind: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    name: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64), unique=True)
    description: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(255), default="")
    body: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.Text, default="")
    # whose chat changed it last; no FK, like the log
    opportunity_id: sa_orm.Mapped[int | None]
    updated_at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(
        default=lambda: dt.datetime.now(dt.UTC)
    )


class ClaudeMemoryChange(Base):
    """One change of the memory, by Claude or by you, to see and undo it by hand."""

    __tablename__ = "claude_memory_changes"

    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    name: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(64))
    # added / updated / deleted
    op: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    # claude / you
    by: sa_orm.Mapped[str] = sa_orm.mapped_column(sa.String(16))
    before: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    after: sa_orm.Mapped[str | None] = sa_orm.mapped_column(sa.Text)
    opportunity_id: sa_orm.Mapped[int | None]
    at: sa_orm.Mapped[dt.datetime] = sa_orm.mapped_column(
        default=lambda: dt.datetime.now(dt.UTC), index=True
    )
