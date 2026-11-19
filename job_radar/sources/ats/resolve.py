"""Find the original posting of LinkedIn alert vacancies on the employer's ATS board.

``import_ats`` takes the To review vacancies from LinkedIn alerts without Easy Apply and
without an ATS source, groups them by company and, per company:

1. finds its boards (``find_boards``): a tenant link already in stored data (a source
   record URL or a stored email of the company, e.g. ``jobs.lever.co/<slug>/...``) is
   ``verified``; without one, slugs guessed from the name are tried on every provider, and
   a board the API knows becomes ``verified`` only when its own company name (Greenhouse,
   Workable) equals the company's, ``guessed`` otherwise. A sender domain such as
   ``greenhouse-mail.io`` names the vendor, not the employer, and is never used.
2. reads each board once per run (the run's cache serves every vacancy of the company);
3. runs ``matching.choose`` per vacancy. A certain match on a verified board adds an ATS
   ``SourceRecord`` (``<slug>:<job id>``) to the existing vacancy, stores the description
   with ``dedup.add_snapshot`` and applies the usual rules; everything else is kept in
   ``AtsLookup`` with its candidates. A posting already on another vacancy is not moved,
   and a posting gone from the board never deletes anything.

``confirm_board`` records a board by hand. See docs/ats.md.

Usage:
    import job_radar.sources.ats.resolve as ats_resolve
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.sources.ats.boards as ats_boards
import job_radar.sources.ats.matching as ats_matching

# a vacancy without a match is looked up again after a week
RECHECK = dt.timedelta(days=7)
# a guessed slug the API did not know is not asked again for a month
ABSENT_RECHECK = dt.timedelta(days=30)
SOURCE_TYPES = {p: models.SourceType(p) for p in ats_boards.PROVIDERS}

# Links that name one employer's board (the tenant), in any stored text.
_LINKS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", re.I)),
    (
        "greenhouse",
        re.compile(
            r"(?:job-)?boards(?:-api)?(?:\.eu)?\.greenhouse\.io/(?:v1/boards/)?([\w-]+)", re.I
        ),
    ),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)", re.I)),
    ("lever", re.compile(r"api\.(?:eu\.)?lever\.co/v0/postings/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I)),
]
_NOT_SLUGS = {"embed", "v1", "api", "j", "jobs", "careers", "static", "assets"}


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    # SQLite gives back naive datetimes; they are UTC.
    return value.replace(tzinfo=value.tzinfo or dt.UTC) if value else None


def board_links(text: str) -> list[tuple[str, str]]:
    """(provider, slug) of every employer board link in ``text``, in order, without repeats."""
    found: list[tuple[str, str]] = []
    for provider, pattern in _LINKS:
        for match in pattern.finditer(text or ""):
            slug = match.group(1).strip(".")
            if slug.lower() in _NOT_SLUGS or (provider, slug) in found:
                continue
            found.append((provider, slug))
    return found


def guess_slugs(company: models.Company) -> list[str]:
    """``acme``, ``acmerobotics`` / ``acme-robotics`` from the normalized company name."""
    if company.name.startswith("Unknown ("):
        return []
    words = re.sub(r"[^a-z0-9 ]", "", dedup.normalize_company_name(company.name)).split()
    if not words:
        return []
    return list(dict.fromkeys(["".join(words), "-".join(words)]))


@dataclasses.dataclass
class AtsStats:
    """Counters of one run, for the sync status line."""

    vacancies: int = 0
    matched: int = 0
    ambiguous: int = 0
    no_match: int = 0
    no_board: int = 0
    taken: int = 0
    boards_found: int = 0
    requests: int = 0
    # the request budget ran out; the rest waits for the next run
    limited: bool = False
    errors: list[str] = dataclasses.field(default_factory=list)


def waiting(session: sa_orm.Session, now: dt.datetime) -> list[models.Opportunity]:
    """To review vacancies from LinkedIn alerts, no Easy Apply, no ATS source, and not
    looked up in the last week (an error is retried on the next run). Best first."""
    rows = session.scalars(
        sa.select(models.Opportunity)
        .join(models.SourceRecord)
        .where(
            models.Opportunity.state == models.OpportunityState.NEW,
            models.Opportunity.easy_apply.is_not(True),
            models.SourceRecord.source_type == models.SourceType.LINKEDIN_ALERT,
        )
        .distinct()
    ).all()
    lookups = {
        lk.opportunity_id: lk
        for lk in session.scalars(
            sa.select(models.AtsLookup).where(
                models.AtsLookup.opportunity_id.in_([o.id for o in rows])
            )
        )
    }
    out = []
    for o in rows:
        if any(r.source_type in SOURCE_TYPES.values() for r in o.source_records):
            continue
        lookup = lookups.get(o.id)
        if lookup and lookup.status != "error" and _aware(lookup.checked_at) > now - RECHECK:
            continue
        out.append(o)
    return sorted(out, key=lambda o: -(o.score if o.score is not None else -99))


def _company_texts(session: sa_orm.Session, company: models.Company) -> cabc.Iterator[str]:
    yield from session.scalars(
        sa.select(models.SourceRecord.url)
        .join(models.Opportunity)
        .where(models.Opportunity.company_id == company.id, models.SourceRecord.url.is_not(None))
    )
    for mail in session.scalars(
        sa.select(models.EmailMessage).where(models.EmailMessage.company_id == company.id)
    ):
        yield " ".join(filter(None, (mail.body_html, mail.body_text, mail.snippet)))


def _board(session: sa_orm.Session, provider: str, slug: str) -> models.AtsBoard | None:
    return session.scalar(
        sa.select(models.AtsBoard).where(
            models.AtsBoard.provider == provider,
            sa.func.lower(models.AtsBoard.slug) == slug.lower(),
        )
    )


class _Run:
    """One run: the client, the boards read so far and the counters."""

    def __init__(self, session: sa_orm.Session, client: ats_boards.AtsClient, now: dt.datetime):
        self.session, self.client, self.now = session, client, now
        self.cache: dict[tuple[str, str], ats_boards.Board | Exception] = {}
        self.stats = AtsStats()

    def read(self, board: models.AtsBoard) -> ats_boards.Board | None:
        key = (board.provider, board.slug.lower())
        if key not in self.cache:
            try:
                self.cache[key] = self.client.read_board(board.provider, board.slug)
                board.error = None
            except ats_boards.AtsError as e:
                self.cache[key] = e
                board.error = str(e)[:255]
                self.stats.errors.append(f"{board.provider}/{board.slug}: {e}")
            board.checked_at = self.now
        result = self.cache[key]
        return result if isinstance(result, ats_boards.Board) else None

    def find_boards(self, company: models.Company, guess: bool) -> list[models.AtsBoard]:
        """Usable (verified or guessed) boards of the company; looks for new ones first."""
        session = self.session
        for text in _company_texts(session, company):
            for provider, slug in board_links(text):
                row = _board(session, provider, slug)
                if row is None:
                    row = models.AtsBoard(
                        company=company,
                        provider=provider,
                        slug=slug,
                        status="verified",
                        found_by="link",
                    )
                    session.add(row)
                    self.stats.boards_found += 1
                elif row.company_id == company.id and row.status != "verified":
                    row.status, row.found_by = "verified", "link"
        session.flush()
        usable = self._usable(company)
        if usable or not guess:
            return usable
        for slug in guess_slugs(company):
            for provider in ats_boards.PROVIDERS:
                if self._try_guess(company, provider, slug):
                    return self._usable(company)
        return []

    def _usable(self, company: models.Company) -> list[models.AtsBoard]:
        return list(
            self.session.scalars(
                sa.select(models.AtsBoard)
                .where(
                    models.AtsBoard.company_id == company.id,
                    models.AtsBoard.status.in_(["verified", "guessed"]),
                )
                .order_by(models.AtsBoard.id)
            )
        )

    def _try_guess(self, company: models.Company, provider: str, slug: str) -> bool:
        row = _board(self.session, provider, slug)
        if row is not None and (
            row.company_id != company.id
            or (row.status == "absent" and _aware(row.checked_at) > self.now - ABSENT_RECHECK)
        ):
            return False
        try:
            board = self.client.read_board(provider, slug, want_name=True)
        except ats_boards.BoardNotFound:
            board = None
        except ats_boards.AtsError as e:
            if str(e) != "unexpected response shape":
                self.stats.errors.append(f"{provider}/{slug}: {e}")
                raise
            board = None
        if row is None:
            row = models.AtsBoard(
                company=company, provider=provider, slug=slug, status="absent", found_by="guess"
            )
            self.session.add(row)
        row.checked_at = self.now
        if board is None or not board.postings:
            row.status = "absent"
            self.session.flush()
            return False
        self.cache[(provider, slug.lower())] = board
        row.board_name = board.name
        named = board.name and (
            dedup.normalize_company_name(board.name) in {company.normalized_name, *company.aliases}
        )
        row.status, row.found_by = ("verified", "name") if named else ("guessed", "guess")
        self.stats.boards_found += 1
        self.session.flush()
        return True


def _set_lookup(
    session: sa_orm.Session,
    opportunity: models.Opportunity,
    status: str,
    now: dt.datetime,
    *,
    board: models.AtsBoard | None = None,
    candidates: cabc.Iterable[ats_boards.Posting] = (),
    detail: str | None = None,
) -> None:
    lookup = session.scalar(
        sa.select(models.AtsLookup).where(models.AtsLookup.opportunity_id == opportunity.id)
    ) or models.AtsLookup(opportunity_id=opportunity.id)
    lookup.status, lookup.checked_at = status, now
    lookup.board_id = board.id if board else None
    lookup.candidates = [p.as_candidate() for p in candidates]
    lookup.detail = detail[:255] if detail else None
    session.add(lookup)


def attach(
    session: sa_orm.Session, opportunity: models.Opportunity, posting: ats_boards.Posting
) -> models.SourceRecord | None:
    """Add the posting to the vacancy as a source with its description; None if the posting
    already belongs to another vacancy. Running it again adds nothing new."""
    import job_radar.review.auto_ignore as review_auto_ignore

    source_type = SOURCE_TYPES[posting.provider]
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type == source_type,
            models.SourceRecord.external_id == posting.external_id,
        )
    )
    if record is not None and record.opportunity_id != opportunity.id:
        return None
    if record is None:
        record = models.SourceRecord(
            source_type=source_type,
            external_id=posting.external_id,
            url=posting.url or None,
            opportunity=opportunity,
        )
        session.add(record)
        session.flush()
    if posting.text:
        dedup.add_snapshot(session, record, posting.text)
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)
    return record


def _resolve_company(
    run: _Run, company: models.Company, opportunities: list[models.Opportunity], guess: bool
) -> None:
    session, stats, now = run.session, run.stats, run.now
    try:
        boards = run.find_boards(company, guess)
    except ats_boards.AtsError as e:
        for o in opportunities:
            _set_lookup(session, o, "error", now, detail=str(e))
        return
    if not boards:
        for o in opportunities:
            _set_lookup(session, o, "no_board", now)
        stats.no_board += len(opportunities)
        return
    read = [(b, run.read(b)) for b in boards]
    verified = [
        (b, p) for b, board in read if board and b.status == "verified" for p in board.postings
    ]
    guessed = [
        (b, p) for b, board in read if board and b.status == "guessed" for p in board.postings
    ]
    failed = [b for b, board in read if board is None]
    for o in opportunities:
        board_of = {id(p): b for b, p in verified + guessed}
        decision = ats_matching.choose(o.title, o.location, [p for _, p in verified])
        if decision.status != "matched" and guessed:
            loose = ats_matching.choose(o.title, o.location, [p for _, p in guessed])
            if loose.candidates:
                decision = ats_matching.Decision("ambiguous", candidates=loose.candidates)
                why = "only on a guessed board; confirm it with `job-radar ats confirm`"
                _set_lookup(
                    session,
                    o,
                    "ambiguous",
                    now,
                    board=board_of[id(loose.candidates[0])],
                    candidates=loose.candidates,
                    detail=why,
                )
                stats.ambiguous += 1
                continue
        if decision.status == "matched" and decision.posting is not None:
            board = board_of[id(decision.posting)]
            if attach(session, o, decision.posting) is None:
                _set_lookup(
                    session,
                    o,
                    "taken",
                    now,
                    board=board,
                    candidates=[decision.posting],
                    detail="the posting is already on another vacancy",
                )
                stats.taken += 1
            else:
                _set_lookup(session, o, "matched", now, board=board, candidates=[decision.posting])
                stats.matched += 1
        elif decision.status == "ambiguous":
            _set_lookup(
                session,
                o,
                "ambiguous",
                now,
                board=board_of[id(decision.candidates[0])],
                candidates=decision.candidates,
            )
            stats.ambiguous += 1
        elif failed and not decision.candidates:
            # A board that could not be read is not "no match" (docs/ats.md#errors).
            detail = "; ".join(f"{b.provider}/{b.slug}: {b.error}" for b in failed)
            _set_lookup(session, o, "error", now, detail=detail)
        else:
            _set_lookup(session, o, "no_match", now, candidates=decision.candidates)
            stats.no_match += 1


def import_ats(
    session: sa_orm.Session,
    client: ats_boards.AtsClient,
    *,
    now: dt.datetime | None = None,
    guess: bool = True,
) -> AtsStats:
    now = now or dt.datetime.now(dt.UTC)
    run = _Run(session, client, now)
    todo = waiting(session, now)
    run.stats.vacancies = len(todo)
    by_company: dict[int, list[models.Opportunity]] = {}
    for o in todo:
        by_company.setdefault(o.company_id, []).append(o)
    for company_id, opportunities in by_company.items():
        company = session.get(models.Company, company_id)
        try:
            _resolve_company(run, company, opportunities, guess)
        except ats_boards.BudgetSpent:
            session.rollback()
            run.stats.limited = True
            break
        session.commit()
    run.stats.requests = client.requests
    return run.stats


def confirm_board(
    session: sa_orm.Session, company_name: str, provider: str, slug: str
) -> models.AtsBoard:
    """Record by hand that ``company_name``'s board is ``provider``/``slug``; its vacancies
    without a match are looked up again on the next run."""
    if provider not in ats_boards.PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(ats_boards.PROVIDERS)}")
    company = session.scalar(
        sa.select(models.Company).where(
            models.Company.normalized_name == dedup.normalize_company_name(company_name)
        )
    )
    if company is None:
        raise ValueError(f"no company {company_name!r} in the database")
    row = _board(session, provider, slug) or models.AtsBoard(provider=provider, slug=slug)
    row.company, row.status, row.found_by = company, "verified", "manual"
    session.add(row)
    session.execute(
        sa.delete(models.AtsLookup).where(
            models.AtsLookup.status != "matched",
            models.AtsLookup.opportunity_id.in_(
                sa.select(models.Opportunity.id).where(models.Opportunity.company_id == company.id)
            ),
        )
    )
    session.commit()
    return row


def lookups(
    session: sa_orm.Session, status: str = "ambiguous"
) -> list[tuple[models.AtsLookup, models.Opportunity]]:
    """Lookups with the given status and their vacancies, newest first (for the CLI)."""
    rows = session.execute(
        sa.select(models.AtsLookup, models.Opportunity)
        .join(models.Opportunity, models.Opportunity.id == models.AtsLookup.opportunity_id)
        .where(models.AtsLookup.status == status)
        .order_by(models.AtsLookup.checked_at.desc())
    ).all()
    return [(lk, o) for lk, o in rows]
