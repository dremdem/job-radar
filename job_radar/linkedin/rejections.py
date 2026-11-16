"""Detect rejection emails LinkedIn sends about applications and record them.

These come from jobs-noreply@linkedin.com with a subject like
"Your application to <role> at <company>" and a body that says the application will not
move forward. They are already synced with their body stored (same sender as the alert
digest, see docs/gmail.md), so no extra Gmail query is needed. ``detect_rejection``
reads one email and ``import_rejections`` records a ``Rejection`` per email. See
docs/data-model.md for how a rejection flags the company's vacancies.

Usage:
    import job_radar.linkedin.rejections as linkedin_rejections
"""

import collections.abc as cabc
import dataclasses
import re

import bs4
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.rejections as rejections

# LinkedIn application-update emails; distinct from the alert digest (jobalerts-...).
APPLICATION_SENDER = "jobs-noreply@linkedin.com"
_SUBJECT = re.compile(r"^Your application to (?P<role>.+) at (?P<company>.+)$", re.IGNORECASE)
# Body wording that marks an actual rejection (not "your application was viewed" etc.).
# Seen in real mail: "we will not be moving forward with your application",
# "<company> did not select your application to move forward",
# "we have not selected your application to move forward"; employers' own texts add
# "we regret to inform you", "proceed with another candidate", and Russian wording.
_REJECTION = re.compile(
    r"(will not|won't|not) be (moving|progressing|proceeding)"
    r"|not moving forward with your application"
    r"|decided not to move forward"
    r"|(did not|have not|has not) select(ed)? your application"
    r"|regret to inform"
    r"|(proceed|move forward|moving forward|go) with (an)?other candidates?"
    r"|решили не рассматривать|не готовы продолжить|к сожалению",
    re.IGNORECASE,
)
REJECTION_EVENT = "rejection"
# checked, not a rejection
UPDATE_EVENT = "application_update"

# the message was deleted from Gmail for good; its body can't be read
GONE_EVENT = "gone"

# Returns (html, text) of a message; used for emails stored without a body.
# Raises MessageGone when Gmail no longer has the message (404).
FetchBody = cabc.Callable[[str], tuple[str | None, str | None]]


class MessageGone(Exception):
    """The message is no longer in Gmail (deleted from Trash), so its body can't be fetched."""


@dataclasses.dataclass(frozen=True)
class RejectionInfo:
    """Role and company of a detected rejection."""

    role: str
    company: str


@dataclasses.dataclass
class RejectionStats:
    """Counters of one rejection import, for the sync status line."""

    emails: int = 0
    detected: int = 0
    new: int = 0
    # bodies fetched from Gmail for emails stored without one
    fetched: int = 0
    # bodyless emails left for a run with Gmail access
    pending: int = 0
    # deleted from Gmail before the body was read
    gone: int = 0


def _text(html: str | None, text: str | None) -> str:
    # The plain part of LinkedIn updates is a stub; the sentence that matters is in the HTML.
    parts = []
    if html:
        parts.append(bs4.BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    if text:
        parts.append(text)
    return "\n".join(parts)


def detect_rejection(subject: str, body: str) -> RejectionInfo | None:
    """Return role and company if this looks like a rejection, else None."""
    match = _SUBJECT.match(subject.strip())
    if not match:
        return None
    if not _REJECTION.search(body):
        return None
    return RejectionInfo(role=match["role"].strip(), company=match["company"].strip())


def import_rejections(
    session: sa_orm.Session, fetch_body: FetchBody | None = None, *, recheck: bool = False
) -> RejectionStats:
    """Record a Rejection for every rejection email. Idempotent by email id.

    Emails stored without a body are checked by fetching it with ``fetch_body``; only the
    result is kept (``detected_event``), the body is never written. ``recheck`` re-examines
    emails an earlier run marked as not a rejection (after the rules improve). See docs/gmail.md.
    """
    skip = (REJECTION_EVENT, GONE_EVENT) if recheck else (REJECTION_EVENT, UPDATE_EVENT, GONE_EVENT)
    stats = RejectionStats()
    emails = session.scalars(
        sa.select(models.EmailMessage).where(
            models.EmailMessage.from_addr.contains(APPLICATION_SENDER)
        )
    ).all()
    for email in emails:
        stats.emails += 1
        if not _SUBJECT.match(email.subject.strip()) or email.hidden_at is not None:
            # hidden: deleted from the company history (#225)
            continue
        html, text = email.body_html, email.body_text
        if html is None and text is None:
            if email.detected_event in skip:
                continue
            if fetch_body is None:
                stats.pending += 1
                continue
            try:
                html, text = fetch_body(email.gmail_message_id)
            except MessageGone:
                email.detected_event = GONE_EVENT
                stats.gone += 1
                session.commit()
                continue
            stats.fetched += 1
        info = detect_rejection(email.subject, _text(html, text))
        if info is None:
            email.detected_event = UPDATE_EVENT
            session.commit()
            continue
        stats.detected += 1
        already = session.scalar(
            sa.select(models.Rejection.id).where(models.Rejection.email_message_id == email.id)
        )
        company = dedup.get_or_create_company(session, info.company)
        email.detected_event = REJECTION_EVENT
        email.company_id = company.id
        email.company_match = "exact"
        rejections.mark_rejected(
            session,
            company_id=company.id,
            source=models.RejectionSource.GMAIL,
            role=info.role,
            rejected_at=email.sent_at,
            email_message_id=email.id,
        )
        if already is None:
            stats.new += 1
        # Commit per email: fetched results survive an interrupted run.
        session.commit()
    session.commit()
    return stats
