"""Past applications reconstructed from mail. See docs/applications.md.

LinkedIn confirmations are facts and are stored as confirmed; ATS confirmations are
recognized by heuristics and stored as suggestions for the user to confirm or dismiss.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import get_or_create_company
from .linkedin.rejections import FetchBody, detect_rejection
from .models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    EmailDirection,
    EmailMessage,
    Rejection,
    SourceRecord,
    SourceType,
)

CONFIRMATION_EVENT = "application_confirmation"
# One application usually produces a LinkedIn and an ATS email a day or two apart.
SAME_APPLICATION_WINDOW = timedelta(days=3)

LINKEDIN_SENDER = "jobs-noreply@linkedin.com"
_LINKEDIN_SUBJECT = re.compile(r"your application was sent to (?P<company>.+)$", re.IGNORECASE)
_JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")

ATS_DOMAINS = (
    "workablemail.com",
    "ashbyhq.com",
    "lever.co",
    "greenhouse-mail.io",
    "greenhouse.io",
    "teamtailor-mail.com",
    "smartrecruiters.com",
    "myworkday.com",
    "applytojob.com",
    "recruitee.com",
    "bamboohr.com",
    "personio.de",
    "breezy.hr",
    "jobvite.com",
    "icims.com",
    "hibob.com",  # "… (via Bob)": many employers send through it (#178)
)
# Subdomain labels that name the ATS region/product, not the company.
_GENERIC_LABELS = {"us", "eu", "candidates", "inbound", "hire", "mail", "email", "jobs", "app"}
_GENERIC_NAMES = re.compile(
    r"^(no-?reply|notifications?|workable|greenhouse|lever|ashby|teamtailor|smartrecruiters"
    r"|workday|jazzhr|recruitee|bamboohr|personio|breezy|jobvite|icims|bob|hibob)$",
    re.IGNORECASE,
)
_NAME_PREFIX = re.compile(r"^(careers|jobs|recruiting|talent)\s*(@|at)\s*", re.IGNORECASE)
_NAME_SUFFIX = re.compile(
    r"\s*(hiring team|recruiting( team)?|talent( acquisition)?( team)?|careers|jobs|hr|"
    r"people( team)?|no[- ]?reply)\s*$",
    re.IGNORECASE,
)
# "Jane - Northwind", "WLG Team \u2013 WLG": the company is the last part.
_NAME_SEPARATOR = re.compile("\\s+[-\u2013\u2014]\\s+")
# Later mail about an existing application (usually a rejection), not a new application.
_STATUS_UPDATE = re.compile(
    r"application update|update on your application|information about your .*application"
    r"|follow up on your interest|regarding your application",
    re.IGNORECASE,
)
_ATS_WORDS = r"(?:workday|icims|greenhouse|lever|ashby|workable|smartrecruiters|teamtailor)"
# "Paychex @ icims", "Workday@intive": drop the ATS side.
_ATS_WRAPPER = re.compile(rf"^\s*{_ATS_WORDS}\s*@\s*|\s*@\s*{_ATS_WORDS}\s*$", re.IGNORECASE)
_CONFIRMATION = re.compile(
    r"thanks?( you)? for (applying|your application|submitting)"
    r"|received your (application|resume|cv)"
    r"|application (has been |was )?received"
    r"|your application is received",
    re.IGNORECASE,
)
_SUBJECT_COMPANY = [
    # HiBob: "Your application at Pure App was received", "Your Zepz Application for …" (#178).
    re.compile(
        r"your application at (?P<c>.+?)(?: was received| [\u2013\u2014-] |$)", re.IGNORECASE
    ),
    re.compile(r"\byour (?P<c>[^!.|,]+?) application for\b", re.IGNORECASE),
    # "application to <x>" usually names the role, so only "applying/applied to <company>".
    re.compile(r"(?:applying|applied) (?:to|at|with) (?P<c>[^!.|,]+)", re.IGNORECASE),
    re.compile(r" at (?P<c>[^!.|]+?)\s*!?$", re.IGNORECASE),
]
# A role never runs past the end of a sentence (but may contain "Sr." or "C#").
_R = r"(?P<r>(?:(?!\. [A-Z]|[!?\n]).)+?)"
_ROLE = [
    re.compile(rf"applying (?:to|for) the {_R} (?:position|opportunity|role|job)\b", re.IGNORECASE),
    re.compile(rf"for the position of (?:the )?{_R}(?: position)?[.,!]", re.IGNORECASE),
    re.compile(rf"applying to {_R} at [^!.]+", re.IGNORECASE),
    re.compile("application\\s*[\u2014\u2013-]\\s*" + _R + " at ", re.IGNORECASE),
]
_ROLE_PREFIX = re.compile(
    r"^(?:the )?(?:opportunity|position|role) (?:of|as) (?:our |the |an? )?", re.I
)


@dataclass(frozen=True)
class DetectedApplication:
    source: ApplicationSource
    company: str
    role: str | None
    linkedin_job_id: str | None = None
    needs_body: bool = False  # LinkedIn mail stored without a body: role is in the body


def _linkedin_details(html: str | None, text: str | None) -> tuple[str | None, str | None]:
    """(role, job id) from a LinkedIn confirmation: the first job link is the applied job."""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        anchors = [a for a in soup.find_all("a", href=True) if _JOB_ID.search(a["href"])]
        if anchors:
            job_id = _JOB_ID.search(anchors[0]["href"]).group(1)
            texts = [
                a.get_text(" ", strip=True) for a in anchors if f"jobs/view/{job_id}" in a["href"]
            ]
            role = min((t for t in texts if t), key=len, default=None)
            return role, job_id
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines[:-1]):
            if "was sent to" in line.lower():
                return lines[i + 1], None
    return None, None


_ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")


def ats_subject_company(subject: str) -> str | None:
    """The employer named in an ATS mail's subject, or None."""
    subject = _ZERO_WIDTH.sub("", subject).strip()
    for pattern in _SUBJECT_COMPANY:
        if match := pattern.search(subject):
            return re.sub(r"^join\s+", "", match["c"].strip(), flags=re.IGNORECASE)
    return None


def _ats_company(display: str, domain: str, subject: str) -> str | None:
    if name := ats_subject_company(subject):
        return name
    name = _ATS_WRAPPER.sub("", display.strip().strip('"'))
    name = _NAME_PREFIX.sub("", name)
    name = _NAME_SEPARATOR.split(name)[-1]
    name = _NAME_SUFFIX.sub("", name).strip()
    if name and not _GENERIC_NAMES.match(name):
        return name
    base = next(d for d in ATS_DOMAINS if domain.endswith(d))
    labels = domain[: -len(base)].rstrip(".").split(".") if domain != base else []
    labels = [label for label in labels if label and label not in _GENERIC_LABELS]
    return labels[0] if labels else None


def _ats_role(subject: str, snippet: str, company: str) -> str | None:
    for text in (snippet, subject):
        for pattern in _ROLE:
            if match := pattern.search(text):
                return _ROLE_PREFIX.sub("", match["r"].strip())
    if match := re.match(rf"^(?P<r>.+?) - {re.escape(company)}$", subject.strip()):
        return match["r"].strip()
    return None


def _split_sender(from_addr: str) -> tuple[str, str]:
    # parseaddr trips over "@" in a display name ("Careers @ Hugging Face <...>").
    if match := re.match(r"^(?P<display>.*)<(?P<address>[^<>]+)>\s*$", from_addr.strip()):
        return match["display"].strip().strip('"'), match["address"].strip().lower()
    display, address = parseaddr(from_addr)
    return display, address.lower()


def detect_application(
    from_addr: str, subject: str, snippet: str, html: str | None, text: str | None
) -> DetectedApplication | None:
    display, address = _split_sender(from_addr)
    subject = subject.strip()
    if address == LINKEDIN_SENDER:
        match = _LINKEDIN_SUBJECT.search(subject)
        if not match:
            return None
        role, job_id = _linkedin_details(html, text)
        return DetectedApplication(
            source=ApplicationSource.LINKEDIN,
            company=match["company"].strip(),
            role=role,
            linkedin_job_id=job_id,
            needs_body=html is None and text is None,
        )
    domain = address.rpartition("@")[2]
    if not any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        return None
    probe = f"{subject}\n{snippet}"
    if (
        _STATUS_UPDATE.search(subject)
        or not _CONFIRMATION.search(probe)
        or detect_rejection("Your application to x at y", probe)
    ):
        return None
    company = _ats_company(display, domain, subject)
    if not company:
        return None
    return DetectedApplication(
        source=ApplicationSource.ATS, company=company, role=_ats_role(subject, snippet, company)
    )


@dataclass
class ImportStats:
    emails: int = 0
    detected: int = 0
    new: int = 0
    linked: int = 0  # emails attached to an application found in another email
    fetched: int = 0
    pending: int = 0  # LinkedIn confirmations whose role waits for Gmail access


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _nearby_application(session: Session, company_id: int, at: datetime) -> Application | None:
    at = _as_utc(at)
    for app in session.scalars(select(Application).where(Application.company_id == company_id)):
        if abs(_as_utc(app.applied_at) - at) <= SAME_APPLICATION_WINDOW:
            return app
    return None


def _opportunity_for(session: Session, job_id: str | None) -> int | None:
    if job_id is None:
        return None
    return session.scalar(
        select(SourceRecord.opportunity_id).where(
            SourceRecord.source_type == SourceType.LINKEDIN_ALERT,
            SourceRecord.external_id == job_id,
        )
    )


def import_applications(session: Session, fetch_body: FetchBody | None = None) -> ImportStats:
    """Create applications from confirmation emails. Idempotent by email id.

    LinkedIn confirmations stored without a body are recorded right away; their role is
    filled in once the body can be fetched (``fetch_body``), and the body is never stored.
    """
    stats = ImportStats()
    emails = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.direction == EmailDirection.IN,
            EmailMessage.detected_event.is_(None),
            EmailMessage.hidden_at.is_(None),  # deleted from the company history (#225)
        )
        .order_by(EmailMessage.sent_at)
    ).all()
    for email in emails:
        stats.emails += 1
        html, text = email.body_html, email.body_text
        found = detect_application(email.from_addr, email.subject, email.snippet, html, text)
        if found is None:
            continue
        stats.detected += 1
        if found.needs_body and fetch_body is not None:
            html, text = fetch_body(email.gmail_message_id)
            stats.fetched += 1
            found = detect_application(email.from_addr, email.subject, email.snippet, html, text)
        waiting = found.needs_body

        app = session.scalar(select(Application).where(Application.email_message_id == email.id))
        company = app.company if app else get_or_create_company(session, found.company)
        if app is None:
            app = _nearby_application(session, company.id, email.sent_at)
            if app is not None:
                stats.linked += 1
        if app is None:
            app = Application(
                company_id=company.id,
                applied_at=email.sent_at,
                source=found.source,
                review=(
                    ApplicationReview.CONFIRMED
                    if found.source is ApplicationSource.LINKEDIN
                    else ApplicationReview.SUGGESTED
                ),
                email_message_id=email.id,
            )
            session.add(app)
            stats.new += 1
        if found.source is ApplicationSource.LINKEDIN and app.review is ApplicationReview.SUGGESTED:
            # LinkedIn states the application as a fact; a dismissal by the user still wins.
            app.review = ApplicationReview.CONFIRMED
        app.role = app.role or found.role
        app.linkedin_job_id = app.linkedin_job_id or found.linkedin_job_id
        app.opportunity_id = app.opportunity_id or _opportunity_for(session, found.linkedin_job_id)
        email.company_id = company.id
        email.company_match = "exact"
        if waiting:
            stats.pending += 1  # revisited on a run with Gmail access
        else:
            email.detected_event = CONFIRMATION_EVENT
        session.commit()
    return stats


def application_status(session: Session, app: Application) -> str:
    """`rejected` when the company rejected on or after the application date."""
    rejected = session.scalar(
        select(Rejection.id).where(
            Rejection.company_id == app.company_id, Rejection.rejected_at >= app.applied_at
        )
    )
    return "rejected" if rejected is not None else app.status


def _set_review(session: Session, app_id: int, review: ApplicationReview) -> Application | None:
    app = session.get(Application, app_id)
    if app is not None:
        app.review = review
        session.commit()
    return app


def confirm(session: Session, app_id: int) -> Application | None:
    return _set_review(session, app_id, ApplicationReview.CONFIRMED)


def dismiss(session: Session, app_id: int) -> Application | None:
    return _set_review(session, app_id, ApplicationReview.DISMISSED)


def confirm_all(session: Session, company: str = "") -> int:
    """Confirm every suggestion, or only those whose company contains ``company``."""
    needle = company.strip().casefold()
    suggested = session.scalars(
        select(Application).where(Application.review == ApplicationReview.SUGGESTED)
    ).all()
    chosen = [a for a in suggested if needle in a.company.name.casefold()]
    for a in chosen:
        a.review = ApplicationReview.CONFIRMED
    session.commit()
    return len(chosen)
