"""Past applications reconstructed from confirmation mail.

LinkedIn confirmations are facts and are stored as confirmed applications. ATS
confirmations (Greenhouse, Lever, Workable and the like) are recognized by heuristics and
stored as suggestions that the user confirms or dismisses in the queue.

Main entry points: ``detect_application`` reads one email, ``import_applications`` turns
stored mail into ``Application`` rows (idempotent by email id), ``application_status``
says whether an application was rejected later, and ``confirm`` / ``dismiss`` /
``confirm_all`` resolve suggestions. See docs/applications.md.

Usage:
    import job_radar.applications as applications
"""

import dataclasses
import datetime as dt
import email.utils as email_utils
import re

import bs4
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.mail as mail
import job_radar.models as models
import job_radar.rejections as rejections

CONFIRMATION_EVENT = "application_confirmation"
# One application usually produces a LinkedIn and an ATS email a day or two apart.
SAME_APPLICATION_WINDOW = dt.timedelta(days=3)

LINKEDIN_SENDER = "jobs-noreply@linkedin.com"
_LINKEDIN_SUBJECT = re.compile(r"your application was sent to (?P<company>.+)$", re.IGNORECASE)
_JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")

FDETIMES_DOMAIN = "fdetimes.net"
_FDETIMES_SUBJECT = re.compile(r"^application received:\s*(?P<rest>.+ at .+)$", re.IGNORECASE)
# What the contact step may have called a confirmation that came before #15.
_RELABEL = ("interview", "other", "recruiter_outreach")

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
    # "… (via Bob)": many employers send through it (#178)
    "hibob.com",
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


@dataclasses.dataclass(frozen=True)
class DetectedApplication:
    """What one confirmation email says: source, company, role and, for LinkedIn, the job id."""

    source: models.ApplicationSource
    company: str
    role: str | None
    linkedin_job_id: str | None = None
    # LinkedIn mail stored without a body: role is in the body
    needs_body: bool = False


def _linkedin_details(html: str | None, text: str | None) -> tuple[str | None, str | None]:
    """(role, job id) from a LinkedIn confirmation: the first job link is the applied job."""
    if html:
        soup = bs4.BeautifulSoup(html, "html.parser")
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
    display, address = email_utils.parseaddr(from_addr)
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
            source=models.ApplicationSource.LINKEDIN,
            company=match["company"].strip(),
            role=role,
            linkedin_job_id=job_id,
            needs_body=html is None and text is None,
        )
    domain = address.rpartition("@")[2]
    if domain == FDETIMES_DOMAIN or domain.endswith("." + FDETIMES_DOMAIN):
        # "Application received: <role> at <company>"; a role may contain " at " itself.
        if not (match := _FDETIMES_SUBJECT.match(subject)):
            return None
        role, _, company = match["rest"].rpartition(" at ")
        return DetectedApplication(
            source=models.ApplicationSource.FDETIMES, company=company.strip(), role=role.strip()
        )
    if not any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        return None
    probe = f"{subject}\n{snippet}"
    if (
        _STATUS_UPDATE.search(subject)
        or not _CONFIRMATION.search(probe)
        or rejections.WORDING.search(probe)
    ):
        return None
    company = _ats_company(display, domain, subject)
    if not company:
        return None
    return DetectedApplication(
        source=models.ApplicationSource.ATS,
        company=company,
        role=_ats_role(subject, snippet, company),
    )


@dataclasses.dataclass
class ImportStats:
    """Counters of one ``import_applications`` run, for the sync status line."""

    emails: int = 0
    detected: int = 0
    new: int = 0
    # emails attached to an application found in another email
    linked: int = 0
    fetched: int = 0
    # LinkedIn confirmations whose role waits for Gmail access
    pending: int = 0
    # deleted from Gmail before the body was read; role stays unknown
    gone: int = 0


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _nearby_application(
    session: sa_orm.Session, company_id: int, at: dt.datetime
) -> models.Application | None:
    at = _as_utc(at)
    for app in session.scalars(
        sa.select(models.Application).where(models.Application.company_id == company_id)
    ):
        if abs(_as_utc(app.applied_at) - at) <= SAME_APPLICATION_WINDOW:
            return app
    return None


def _opportunity_for(session: sa_orm.Session, job_id: str | None) -> int | None:
    if job_id is None:
        return None
    return session.scalar(
        sa.select(models.SourceRecord.opportunity_id).where(
            models.SourceRecord.source_type == models.SourceType.LINKEDIN_ALERT,
            models.SourceRecord.external_id == job_id,
        )
    )


def _fdetimes_vacancy(session: sa_orm.Session, company_id: int, role: str) -> models.Opportunity:
    """The vacancy an FDE Times confirmation is about: the FDE Times one of the company with
    this title, else another one with this title, else a new one. Marked Applied; other
    copies with the title join it (#269)."""
    import job_radar.applied_duplicates as applied_duplicates
    import job_radar.dedup as dedup

    same = [
        o
        for o in session.scalars(
            sa.select(models.Opportunity)
            .where(models.Opportunity.company_id == company_id)
            .order_by(models.Opportunity.id.desc())
        )
        if applied_duplicates.same_title(o.title) == applied_duplicates.same_title(role)
    ]
    ranked = sorted(
        same,
        key=lambda o: (
            not any(r.source_type is models.SourceType.FDETIMES for r in o.source_records),
            o.state is not models.OpportunityState.APPLIED,
        ),
    )
    if ranked:
        keep = ranked[0]
    else:
        keep = models.Opportunity(title=role[:255], company_id=company_id)
        session.add(keep)
    if keep.state is not models.OpportunityState.APPLIED:
        keep.state = models.OpportunityState.APPLIED
        keep.auto_ignore_reason = None
    session.flush()
    now = dt.datetime.now(dt.UTC)
    for dup in ranked[1:]:
        # "interested" or applied: your own call, kept apart
        if dup.state in applied_duplicates.MERGEABLE:
            applied_duplicates._seen_again(session, keep, dup, now)
            dedup.merge_opportunities(session, keep, dup)
    return keep


def import_applications(
    session: sa_orm.Session, fetch_body: mail.FetchBody | None = None
) -> ImportStats:
    """Create applications from confirmation emails. Idempotent by email id.

    LinkedIn confirmations stored without a body are recorded right away; their role is
    filled in once the body can be fetched (``fetch_body``), and the body is never stored.
    """
    stats = ImportStats()
    emails = session.scalars(
        sa.select(models.EmailMessage)
        .where(
            models.EmailMessage.direction == models.EmailDirection.IN,
            sa.or_(
                models.EmailMessage.detected_event.is_(None),
                sa.and_(
                    models.EmailMessage.from_addr.ilike(f"%@{FDETIMES_DOMAIN}%"),
                    models.EmailMessage.detected_event.in_(_RELABEL),
                ),
            ),
            # deleted from the company history (#225)
            models.EmailMessage.hidden_at.is_(None),
        )
        .order_by(models.EmailMessage.sent_at)
    ).all()
    for email in emails:
        stats.emails += 1
        html, text = email.body_html, email.body_text
        found = detect_application(email.from_addr, email.subject, email.snippet, html, text)
        if found is None:
            continue
        stats.detected += 1
        gone = False
        if found.needs_body and fetch_body is not None:
            try:
                html, text = fetch_body(email.gmail_message_id)
            except mail.MessageGone:
                gone = True
                stats.gone += 1
            else:
                stats.fetched += 1
                found = detect_application(
                    email.from_addr, email.subject, email.snippet, html, text
                )
        waiting = found.needs_body and not gone

        app = session.scalar(
            sa.select(models.Application).where(models.Application.email_message_id == email.id)
        )
        company = app.company if app else dedup.get_or_create_company(session, found.company)
        if app is None:
            app = _nearby_application(session, company.id, email.sent_at)
            if app is not None:
                stats.linked += 1
        if app is None:
            app = models.Application(
                company_id=company.id,
                applied_at=email.sent_at,
                source=found.source,
                review=(
                    models.ApplicationReview.CONFIRMED
                    if found.source
                    in (models.ApplicationSource.LINKEDIN, models.ApplicationSource.FDETIMES)
                    else models.ApplicationReview.SUGGESTED
                ),
                email_message_id=email.id,
            )
            session.add(app)
            stats.new += 1
        confirmed = (models.ApplicationSource.LINKEDIN, models.ApplicationSource.FDETIMES)
        if found.source in confirmed and app.review is models.ApplicationReview.SUGGESTED:
            # LinkedIn and FDE Times state it as a fact; a dismissal by the user still wins.
            app.review = models.ApplicationReview.CONFIRMED
        app.role = app.role or found.role
        app.linkedin_job_id = app.linkedin_job_id or found.linkedin_job_id
        app.opportunity_id = app.opportunity_id or _opportunity_for(session, found.linkedin_job_id)
        if found.source is models.ApplicationSource.FDETIMES and found.role:
            app.opportunity_id = app.opportunity_id or (
                _fdetimes_vacancy(session, company.id, found.role).id
            )
        email.company_id = company.id
        email.company_match = "exact"
        if waiting:
            # revisited on a run with Gmail access
            stats.pending += 1
        else:
            email.detected_event = mail.GONE_EVENT if gone else CONFIRMATION_EVENT
        session.commit()
    return stats


def application_status(session: sa_orm.Session, app: models.Application) -> str:
    """`rejected` when the company rejected on or after the application date."""
    rejected = session.scalar(
        sa.select(models.Rejection.id).where(
            models.Rejection.company_id == app.company_id,
            models.Rejection.rejected_at >= app.applied_at,
        )
    )
    return "rejected" if rejected is not None else app.status


def _set_review(
    session: sa_orm.Session, app_id: int, review: models.ApplicationReview
) -> models.Application | None:
    app = session.get(models.Application, app_id)
    if app is not None:
        app.review = review
        session.commit()
    return app


def confirm(session: sa_orm.Session, app_id: int) -> models.Application | None:
    return _set_review(session, app_id, models.ApplicationReview.CONFIRMED)


def dismiss(session: sa_orm.Session, app_id: int) -> models.Application | None:
    return _set_review(session, app_id, models.ApplicationReview.DISMISSED)


def confirm_all(session: sa_orm.Session, company: str = "") -> int:
    """Confirm every suggestion, or only those whose company contains ``company``."""
    needle = company.strip().casefold()
    suggested = session.scalars(
        sa.select(models.Application).where(
            models.Application.review == models.ApplicationReview.SUGGESTED
        )
    ).all()
    chosen = [a for a in suggested if needle in a.company.name.casefold()]
    for a in chosen:
        a.review = models.ApplicationReview.CONFIRMED
    session.commit()
    return len(chosen)
