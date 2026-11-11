"""Detect rejection emails LinkedIn sends about applications and record them.

These come from jobs-noreply@linkedin.com with a subject like
"Your application to <role> at <company>" and a body that says the application
will not move forward. They are already synced with their body stored (same
sender as the alert digest, see docs/gmail.md), so no extra Gmail query is needed.
See docs/data-model.md for how a rejection flags the company's opportunities.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import get_or_create_company
from ..models import EmailMessage, Rejection, RejectionSource
from ..rejections import mark_rejected

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
UPDATE_EVENT = "application_update"  # checked, not a rejection

GONE_EVENT = "gone"  # the message was deleted from Gmail for good; its body can't be read

# Returns (html, text) of a message; used for emails stored without a body.
# Raises MessageGone when Gmail no longer has the message (404).
FetchBody = Callable[[str], tuple[str | None, str | None]]


class MessageGone(Exception):
    """The message is no longer in Gmail (deleted from Trash), so its body can't be fetched."""


@dataclass(frozen=True)
class RejectionInfo:
    role: str
    company: str


@dataclass
class RejectionStats:
    emails: int = 0
    detected: int = 0
    new: int = 0
    fetched: int = 0  # bodies fetched from Gmail for emails stored without one
    pending: int = 0  # bodyless emails left for a run with Gmail access
    gone: int = 0  # deleted from Gmail before the body was read


def _text(html: str | None, text: str | None) -> str:
    # The plain part of LinkedIn updates is a stub; the sentence that matters is in the HTML.
    parts = []
    if html:
        parts.append(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
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
    session: Session, fetch_body: FetchBody | None = None, *, recheck: bool = False
) -> RejectionStats:
    """Record a Rejection for every rejection email. Idempotent by email id.

    Emails stored without a body are checked by fetching it with ``fetch_body``; only the
    result is kept (``detected_event``), the body is never written. ``recheck`` re-examines
    emails an earlier run marked as not a rejection (after the rules improve). See docs/gmail.md.
    """
    skip = (REJECTION_EVENT, GONE_EVENT) if recheck else (REJECTION_EVENT, UPDATE_EVENT, GONE_EVENT)
    stats = RejectionStats()
    emails = session.scalars(
        select(EmailMessage).where(EmailMessage.from_addr.contains(APPLICATION_SENDER))
    ).all()
    for email in emails:
        stats.emails += 1
        if not _SUBJECT.match(email.subject.strip()) or email.hidden_at is not None:
            continue  # hidden: deleted from the company history (#225)
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
        already = session.scalar(select(Rejection.id).where(Rejection.email_message_id == email.id))
        company = get_or_create_company(session, info.company)
        email.detected_event = REJECTION_EVENT
        email.company_id = company.id
        email.company_match = "exact"
        mark_rejected(
            session,
            company_id=company.id,
            source=RejectionSource.GMAIL,
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
