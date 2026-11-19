"""Original postings on employer ATS boards (#2, docs/ats.md): the API parsers, the client's
error handling, matching, and the run that enriches alert vacancies. Offline: the fixtures
in tests/fixtures/ats are written in the documented response shapes (see its README).
"""

import datetime as dt
import json

import paths
import pytest
import sqlalchemy as sa

import job_radar.config as config
import job_radar.dedup as dedup
import job_radar.jobs.pipeline as jobs_pipeline
import job_radar.models as models
import job_radar.sources.ats.boards as ats_boards
import job_radar.sources.ats.matching as ats_matching
import job_radar.sources.ats.resolve as ats_resolve

FIXTURES = paths.FIXTURES / "ats"
NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def raw(name: str) -> str:
    return (FIXTURES / name).read_text()


GH_JOBS = ats_boards.jobs_url("greenhouse", "acmerobotics")
GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/acmerobotics"
LEVER = ats_boards.jobs_url("lever", "globex")


class FakeHttp:
    """Answers known URLs from a dict and 404 for the rest; remembers every request."""

    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.answers, self.calls = answers, []

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        return self.answers.get(url, (404, ""))


def client(answers: dict[str, tuple[int, str]], **kw) -> ats_boards.AtsClient:
    return ats_boards.AtsClient(FakeHttp(answers), sleep=lambda s: None, **kw)


def alert(session, company: str, title: str, location: str | None, *, easy_apply=False, n=1):
    opportunity = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, company),
        location=location,
        easy_apply=easy_apply,
        score=1.0,
    )
    session.add(
        models.SourceRecord(
            source_type=models.SourceType.LINKEDIN_ALERT,
            external_id=f"{company}-{title}-{n}",
            url=f"https://www.linkedin.com/jobs/view/{n}",
            opportunity=opportunity,
        )
    )
    session.commit()
    return opportunity


def lookup(session, opportunity) -> models.AtsLookup:
    return session.scalar(
        sa.select(models.AtsLookup).where(models.AtsLookup.opportunity_id == opportunity.id)
    )


def ats_records(session) -> list[models.SourceRecord]:
    return list(
        session.scalars(
            sa.select(models.SourceRecord).where(
                models.SourceRecord.source_type.in_(list(ats_resolve.SOURCE_TYPES.values()))
            )
        )
    )


def board_link(session, company: str, url: str) -> None:
    """A vacancy of the company seen elsewhere whose link points to its board."""
    opportunity = models.Opportunity(
        title="Other role",
        company=dedup.get_or_create_company(session, company),
        state=models.OpportunityState.IGNORE,
    )
    session.add(
        models.SourceRecord(
            source_type=models.SourceType.MANUAL, external_id=url, url=url, opportunity=opportunity
        )
    )
    session.commit()


# --- parsers ----------------------------------------------------------------


def test_greenhouse_jobs_with_unescaped_html_text():
    postings = ats_boards.parse_greenhouse("acmerobotics", load("greenhouse_jobs.json"))
    assert len(postings) == 5
    first = postings[0]
    assert (first.id, first.title, first.location) == (
        "4011001",
        "Senior Python Engineer",
        "Berlin, Germany",
    )
    assert first.external_id == "acmerobotics:4011001"
    assert first.url == "https://job-boards.greenhouse.io/acmerobotics/jobs/4011001"
    assert "We build robots." in first.text and "• 5+ years of Python" in first.text
    assert "<" not in first.text
    # An empty location is unknown, not "anywhere".
    assert postings[4].location is None


def test_lever_text_keeps_lists_and_additional():
    posting = ats_boards.parse_lever("globex", load("lever.json"))[0]
    assert (posting.title, posting.location, posting.remote) == (
        "Sr. Python Developer",
        "Remote, Poland",
        True,
    )
    assert posting.url == "https://jobs.lever.co/globex/5a1c2b3d-0000-4000-8000-000000000001"
    for part in ("Globex builds billing", "What you will need", "Python 3.12", "Django",
                 "Kubernetes", "We hire in Poland only."):  # fmt: skip
        assert part in posting.text


def test_ashby_leaves_out_unlisted_and_joins_locations():
    postings = ats_boards.parse_ashby("initech", load("ashby.json"))
    assert [p.title for p in postings] == ["Python Engineer"]
    assert postings[0].location == "Lisbon; Remote (Portugal)"
    assert postings[0].remote is True and postings[0].text == "Initech needs Python."


def test_workable_uses_shortcode_and_board_name():
    board = client({ats_boards.jobs_url("workable", "umbrella"): (200, raw("workable.json"))})
    result = board.read_board("workable", "umbrella")
    assert result.name == "Umbrella Labs"
    posting = result.postings[0]
    assert (posting.id, posting.location, posting.remote) == ("A1B2C3D4E5", "Madrid, Spain", True)
    assert posting.text == "Async Python, AWS."


def test_lever_shape_is_not_greenhouse_shape():
    with pytest.raises(ats_boards.AtsError, match="unexpected response shape"):
        ats_boards.parse_greenhouse("x", load("lever.json"))
    with pytest.raises(ats_boards.AtsError, match="unexpected response shape"):
        ats_boards.parse_lever("x", load("greenhouse_jobs.json"))


# --- client -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((429, ""), "HTTP 429"),
        ((503, ""), "HTTP 503"),
        ((200, "<html>maintenance</html>"), "not JSON"),
    ],
)
def test_errors_are_not_an_empty_board(answer, error):
    with pytest.raises(ats_boards.AtsError, match=error) as caught:
        client({LEVER: answer}).read_board("lever", "globex")
    assert not isinstance(caught.value, ats_boards.BoardNotFound)


def test_missing_board_and_genuinely_empty_board():
    with pytest.raises(ats_boards.BoardNotFound):
        client({}).read_board("lever", "nobody")
    assert client({LEVER: (200, "[]")}).read_board("lever", "globex").postings == []


def test_client_pauses_between_requests_and_keeps_to_its_budget():
    pauses = []
    c = ats_boards.AtsClient(
        FakeHttp({LEVER: (200, "[]")}), sleep=pauses.append, rand=lambda a, b: b, max_requests=2
    )
    c.read_board("lever", "globex")
    c.read_board("lever", "globex")
    with pytest.raises(ats_boards.BudgetSpent):
        c.read_board("lever", "globex")
    assert pauses == [3.0] and c.requests == 2


# --- links and matching -----------------------------------------------------


def test_board_links_name_the_tenant_not_the_vendor():
    text = (
        "From: no-reply@us.greenhouse-mail.io. Apply at "
        "https://job-boards.greenhouse.io/acmerobotics/jobs/4011001?gh_src=x and "
        "https://jobs.lever.co/globex/5a1c-0001/apply, https://jobs.ashbyhq.com/initech, "
        "https://apply.workable.com/umbrella/j/A1B2C3D4E5/, "
        "https://boards.greenhouse.io/embed/job_board?for=hooli"
    )
    assert ats_resolve.board_links(text) == [
        ("greenhouse", "hooli"),
        ("greenhouse", "acmerobotics"),
        ("lever", "globex"),
        ("ashby", "initech"),
        ("workable", "umbrella"),
    ]
    assert ats_resolve.board_links("Thanks for applying! — via greenhouse-mail.io") == []


@pytest.mark.parametrize(
    ("alert", "posting", "expected"),
    [
        ("Berlin, Berlin, Germany", "Berlin, Germany", True),
        ("Germany (Remote)", "Remote - Germany", True),
        ("European Union (Remote)", "Remote", True),
        ("United States (Remote)", "Remote - US", True),
        ("London, England, United Kingdom", "Berlin, Germany", False),
        ("United Kingdom (Remote)", "Remote - United States", False),
        ("Berlin, Germany", None, None),
        (None, "Berlin, Germany", None),
    ],
)
def test_location_compatible(alert, posting, expected):
    assert ats_matching.location_compatible(alert, posting) is expected


def test_same_title_two_locations_picks_the_alerts_one():
    postings = ats_boards.parse_greenhouse("acmerobotics", load("greenhouse_jobs.json"))
    decision = ats_matching.choose("Senior Python Engineer", "Berlin, Germany", postings)
    assert decision.status == "matched" and decision.posting.id == "4011001"
    other = ats_matching.choose("Senior Python Engineer", "Paris, France", postings)
    assert other.status == "no_match" and len(other.candidates) == 2


def test_same_title_same_location_twice_is_ambiguous():
    postings = ats_boards.parse_greenhouse("acmerobotics", load("greenhouse_jobs.json"))
    decision = ats_matching.choose("Backend Engineer", "Europe (Remote)", postings)
    assert decision.status == "ambiguous" and {p.id for p in decision.candidates} == {
        "4011003",
        "4011004",
    }


def test_missing_location_is_unknown_not_a_match():
    postings = ats_boards.parse_greenhouse("acmerobotics", load("greenhouse_jobs.json"))
    assert ats_matching.choose("Data Engineer", "Berlin, Germany", postings).status == "ambiguous"
    assert ats_matching.choose("Data engineer", None, postings).status == "ambiguous"


def test_titles_are_normalized():
    assert ats_matching.same_title("Sr. Python Developer", "Senior Python developer")
    assert not ats_matching.same_title("Python Developer", "Senior Python Developer")


# --- the run ----------------------------------------------------------------


def gh_answers(jobs: str | None = None) -> dict[str, tuple[int, str]]:
    return {
        GH_JOBS: (200, jobs if jobs is not None else raw("greenhouse_jobs.json")),
        GH_BOARD: (200, raw("greenhouse_board.json")),
    }


def test_verified_link_attaches_the_posting_to_the_alert_vacancy(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics/jobs/1")
    vacancy = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany")
    stats = ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    assert (stats.vacancies, stats.matched, stats.requests) == (1, 1, 1)
    board = session.scalar(sa.select(models.AtsBoard))
    assert (board.provider, board.slug, board.status, board.found_by) == (
        "greenhouse",
        "acmerobotics",
        "verified",
        "link",
    )
    session.refresh(vacancy)
    kinds = {r.source_type for r in vacancy.source_records}
    assert kinds == {models.SourceType.LINKEDIN_ALERT, models.SourceType.GREENHOUSE}
    record = next(
        r for r in vacancy.source_records if r.source_type == models.SourceType.GREENHOUSE
    )
    assert record.external_id == "acmerobotics:4011001"
    assert record.url == "https://job-boards.greenhouse.io/acmerobotics/jobs/4011001"
    assert "5+ years of Python" in record.snapshots[0].raw_text
    # Your state and the alert stay as they were.
    assert vacancy.state is models.OpportunityState.NEW
    assert lookup(session, vacancy).status == "matched"


def test_second_run_adds_no_duplicate_record_or_snapshot(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    vacancy = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany")
    ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    again = client(gh_answers())
    stats = ats_resolve.import_ats(session, again, now=NOW + dt.timedelta(days=30))
    assert stats.vacancies == 0 and again.requests == 0
    posting = ats_boards.parse_greenhouse("acmerobotics", load("greenhouse_jobs.json"))[0]
    ats_resolve.attach(session, vacancy, posting)
    session.commit()
    assert len(ats_records(session)) == 1
    assert session.scalar(sa.select(sa.func.count(models.JobSnapshot.id))) == 1


def test_board_is_read_once_for_all_vacancies_of_the_company(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", n=1)
    alert(session, "Acme Robotics", "Senior Python Engineer", "London, United Kingdom", n=2)
    alert(session, "Acme Robotics", "Backend Engineer", "Europe (Remote)", n=3)
    c = client(gh_answers())
    stats = ats_resolve.import_ats(session, c, now=NOW)
    assert c.requests == 1
    assert (stats.matched, stats.ambiguous) == (2, 1)
    assert {r.external_id for r in ats_records(session)} == {
        "acmerobotics:4011001",
        "acmerobotics:4011002",
    }


def test_ambiguous_keeps_candidates_and_attaches_nothing(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    vacancy = alert(session, "Acme Robotics", "Backend Engineer", "Europe (Remote)")
    ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    row = lookup(session, vacancy)
    assert row.status == "ambiguous" and ats_records(session) == []
    assert {c["id"] for c in row.candidates} == {"4011003", "4011004"}
    assert [o.id for _, o in ats_resolve.lookups(session)] == [vacancy.id]


def test_name_guess_is_verified_by_the_boards_own_name(session):
    vacancy = alert(session, "Acme Robotics Inc.", "Senior Python Engineer", "Berlin, Germany")
    c = client(gh_answers())
    stats = ats_resolve.import_ats(session, c, now=NOW)
    assert stats.matched == 1
    board = session.scalar(sa.select(models.AtsBoard).where(models.AtsBoard.status == "verified"))
    assert (board.slug, board.found_by, board.board_name) == (
        "acmerobotics",
        "name",
        "Acme Robotics",
    )
    assert lookup(session, vacancy).status == "matched"
    # Tried in order, stopped at the first board found; misses are remembered.
    assert c._get.calls == [GH_JOBS, GH_BOARD]


def test_guessed_board_gives_candidates_only(session):
    vacancy = alert(session, "Globex", "Senior Python Developer", "Poland (Remote)")
    answers = {LEVER: (200, raw("lever.json"))}
    stats = ats_resolve.import_ats(session, client(answers), now=NOW)
    board = session.scalar(
        sa.select(models.AtsBoard).where(
            models.AtsBoard.slug == "globex", models.AtsBoard.provider == "lever"
        )
    )
    assert (board.status, board.found_by) == ("guessed", "guess")
    row = lookup(session, vacancy)
    assert (row.status, stats.ambiguous, ats_records(session)) == ("ambiguous", 1, [])
    assert "ats confirm" in row.detail
    # Confirmed by hand: the next run attaches it.
    ats_resolve.confirm_board(session, "Globex", "lever", "globex")
    assert lookup(session, vacancy) is None
    stats = ats_resolve.import_ats(session, client(answers), now=NOW)
    assert stats.matched == 1
    assert [r.external_id for r in ats_records(session)] == [
        "globex:5a1c2b3d-0000-4000-8000-000000000001"
    ]


def test_absent_guesses_are_not_asked_again_for_a_month(session):
    vacancy = alert(session, "Nobody Corp", "Python Developer", "Remote")
    c = client({})
    stats = ats_resolve.import_ats(session, c, now=NOW)
    assert stats.no_board == 1 and lookup(session, vacancy).status == "no_board"
    assert len(c._get.calls) == 4
    lookup(session, vacancy).checked_at = NOW - dt.timedelta(days=8)
    session.commit()
    c = client({})
    ats_resolve.import_ats(session, c, now=NOW)
    assert c._get.calls == []


def test_source_error_is_not_no_match_and_is_retried(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    vacancy = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany")
    stats = ats_resolve.import_ats(session, client({GH_JOBS: (429, "")}), now=NOW)
    row = lookup(session, vacancy)
    assert (row.status, stats.no_match) == ("error", 0)
    assert "HTTP 429" in row.detail and stats.errors == ["greenhouse/acmerobotics: HTTP 429"]
    stats = ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    assert stats.matched == 1


def test_empty_board_is_no_match(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    vacancy = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany")
    ats_resolve.import_ats(session, client(gh_answers('{"jobs": []}')), now=NOW)
    assert lookup(session, vacancy).status == "no_match"


def test_removed_posting_deletes_nothing(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", n=1)
    ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    later = alert(session, "Acme Robotics", "Backend Engineer", "Europe (Remote)", n=2)
    ats_resolve.import_ats(session, client(gh_answers('{"jobs": []}')), now=NOW)
    assert [r.external_id for r in ats_records(session)] == ["acmerobotics:4011001"]
    assert session.scalar(sa.select(sa.func.count(models.JobSnapshot.id))) == 1
    assert lookup(session, later).status == "no_match"


def test_posting_on_another_vacancy_is_not_moved(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    first = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", n=1)
    ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    second = alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", n=2)
    stats = ats_resolve.import_ats(session, client(gh_answers()), now=NOW)
    assert stats.taken == 1 and lookup(session, second).status == "taken"
    assert [r.opportunity_id for r in ats_records(session)] == [first.id]


def test_easy_apply_and_decided_vacancies_are_left_alone(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", easy_apply=True)
    ignored = alert(session, "Acme Robotics", "Backend Engineer", "Europe (Remote)", n=2)
    ignored.state = models.OpportunityState.IGNORE
    session.commit()
    c = client(gh_answers())
    assert ats_resolve.import_ats(session, c, now=NOW).vacancies == 0 and c.requests == 0


def test_budget_stops_the_run_and_leaves_the_rest(session):
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    board_link(session, "Globex", "https://jobs.lever.co/globex")
    alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany", n=1)
    alert(session, "Globex", "Sr. Python Developer", "Poland (Remote)", n=2)
    answers = gh_answers() | {LEVER: (200, raw("lever.json"))}
    stats = ats_resolve.import_ats(session, client(answers, max_requests=1), now=NOW)
    assert stats.limited and stats.matched == 1
    stats = ats_resolve.import_ats(session, client(answers), now=NOW)
    assert (stats.vacancies, stats.matched) == (1, 1)


def test_confirm_needs_a_known_company_and_provider(session):
    alert(session, "Globex", "Python Developer", None)
    with pytest.raises(ValueError, match="unknown provider"):
        ats_resolve.confirm_board(session, "Globex", "workday", "globex")
    with pytest.raises(ValueError, match="no company"):
        ats_resolve.confirm_board(session, "Hooli", "lever", "hooli")


# --- sync step --------------------------------------------------------------


def test_sync_step(session):
    lines: list[str] = []
    jobs_pipeline.import_ats(session, config.Settings(ats=False), lines.append)
    assert lines == ["ats: disabled (JOB_RADAR_ATS=false)."]
    board_link(session, "Acme Robotics", "https://job-boards.greenhouse.io/acmerobotics")
    alert(session, "Acme Robotics", "Senior Python Engineer", "Berlin, Germany")
    lines.clear()
    jobs_pipeline.import_ats(session, config.Settings(), lines.append, client=client(gh_answers()))
    assert lines == [
        "ats: 1 vacancies checked, 1 matched, 0 to pick by hand, 0 not found, "
        "0 without a board, 1 requests"
    ]
