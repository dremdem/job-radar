"""Tests for job_radar.ai.analysis: quote checks, verified facts, risks and the
residence rule, with a fake local model (docs/ai.md).
"""

import typing as t

import pytest

import job_radar.ai.analysis as ai_analysis
import job_radar.auto_ignore as auto_ignore
import job_radar.dedup as dedup
import job_radar.models as models

UK_ONLY = (
    "We build payments.\nThe applicant must be based in the UK and able to work fully remotely.\n"
    "Python and FastAPI."
)


class FakeModel:
    """A local model that returns the given answers in order."""

    model = "fake:1b"

    def __init__(self, answers: list[dict[str, t.Any] | Exception]):
        self.answers = answers
        self.prompts: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, t.Any]) -> dict[str, t.Any]:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(mode="remote", mode_quote="", residence=(), residence_quote="") -> dict[str, t.Any]:
    return {
        "work_mode": mode,
        "work_mode_quote": mode_quote,
        "residence": list(residence),
        "residence_quote": residence_quote,
    }


UK_ANSWER = answer(
    "remote",
    "able to work fully remotely",
    ["UK"],
    "The applicant must be based in the UK and able to work fully remotely.",
)


def add_vacancy(session, title="Python Developer", text=UK_ONLY, state=models.OpportunityState.NEW):
    o = models.Opportunity(
        title=title, company=dedup.get_or_create_company(session, "Acme"), score=8.0
    )
    o.state = state
    session.add(o)
    session.flush()
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    if text:
        dedup.add_snapshot(session, record, text)
    session.commit()
    return o


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("The applicant must be based in the UK", True),
        # case, spacing, final dot
        ("the applicant  MUST be based\nin the UK.", True),
        # slip
        ("The applicant must be based in UK and able to work fully remotely", True),
        # invented
        ("Candidates must reside in Germany.", False),
        ("", False),
    ],
)
def test_quote_in_text(quote, found):
    assert ai_analysis.quote_in_text(quote, UK_ONLY) is found


def test_verify_keeps_supported_facts():
    facts = ai_analysis.verify(UK_ANSWER, UK_ONLY)
    assert facts.residence == ["UK"] and facts.work_mode == "remote"


def test_verify_drops_invented_quotes_and_places():
    facts = ai_analysis.verify(
        answer("hybrid", "Three days a week in our Berlin office.", ["UK", "Thailand"], "x y z"),
        UK_ONLY,
    )
    assert facts.work_mode == "unknown" and facts.work_mode_quote == ""
    assert facts.residence == []
    # A place the text never names is dropped even with a real quote.
    facts = ai_analysis.verify(
        answer(residence=["Thailand"], residence_quote=UK_ANSWER["residence_quote"]), UK_ONLY
    )
    assert facts.residence == []


def test_risks():
    hybrid = models.DescriptionAnalysis(
        work_mode="hybrid", work_mode_quote="2 days in office", residence=[]
    )
    [(points, reason)] = ai_analysis.risks(hybrid)
    assert points == -3.0 and reason.text == "AI: hybrid — “2 days in office”"

    uk = models.DescriptionAnalysis(
        work_mode="remote", residence=["UK"], residence_quote="based in the UK"
    )
    [(points, reason)] = ai_analysis.risks(uk)
    assert points == -2.5 and reason.text == "AI: only for UK — “based in the UK”"

    assert (
        ai_analysis.risks(models.DescriptionAnalysis(work_mode="remote", residence=["Worldwide"]))
        == []
    )
    # Without a home in the profile, a country is a restriction like any other.
    [(points, _)] = ai_analysis.risks(
        models.DescriptionAnalysis(residence=["Thailand"], residence_quote="Thailand")
    )
    assert points == -2.5


def test_home_and_its_regions_are_open(thailand):
    for open_place in ("Thailand", "APAC", "Southeast Asia"):
        assert (
            ai_analysis.risks(
                models.DescriptionAnalysis(work_mode="remote", residence=[open_place])
            )
            == []
        )


def test_analyze_pending_stores_once_and_only_for_to_review(session):
    flagged = add_vacancy(session)
    add_vacancy(session, title="No description", text=None)
    add_vacancy(session, title="Mine", state=models.OpportunityState.INTERESTED)

    model = FakeModel([UK_ANSWER])
    stats = ai_analysis.analyze_pending(session, model)
    assert (stats.analyzed, stats.flagged, stats.errors) == (1, 1, 0)
    [analysis] = session.query(models.DescriptionAnalysis).all()
    assert analysis.opportunity_id == flagged.id and analysis.model == "fake:1b"
    assert analysis.residence == ["UK"] and analysis.raw == UK_ANSWER
    assert "must be based in the UK" in model.prompts[0]

    # Already analyzed: nothing to do.
    assert ai_analysis.analyze_pending(session, FakeModel([])).analyzed == 0


def test_analyze_pending_limit_and_errors(session):
    for title in ("A", "B", "C"):
        add_vacancy(session, title=title, text=f"{title}: {UK_ONLY}")
    stats = ai_analysis.analyze_pending(
        session, FakeModel([RuntimeError("bad JSON"), UK_ANSWER]), limit=2
    )
    assert (stats.analyzed, stats.errors, stats.pending) == (1, 1, 1)
    assert stats.last_error == "bad JSON"


def test_ai_rule_auto_ignores(session):
    auto_ignore.save_rules(session, auto_ignore.Rules(ai_location=True))
    o = add_vacancy(session)
    ai_analysis.analyze_pending(session, FakeModel([UK_ANSWER]))
    session.refresh(o)
    assert o.state is models.OpportunityState.AUTO_IGNORE
    assert o.auto_ignore_reason == "AI: only for UK"


def test_ai_rule_is_off_by_default(session):
    o = add_vacancy(session)
    ai_analysis.analyze_pending(session, FakeModel([UK_ANSWER]))
    session.refresh(o)
    assert o.state is models.OpportunityState.NEW


def test_queue_shows_ai_reason_and_lowers_score(client, session):
    o = add_vacancy(session)
    ai_analysis.analyze_pending(session, FakeModel([UK_ANSWER]))
    # 8.0 - 2.5; no Python bonus, the title already says Python.
    assert ">5.5<" in client.get("/").text
    text = client.get(f"/opportunities/{o.id}/panel").text
    assert "AI: only for UK — “The applicant must be based in the UK" in text


def test_settings_offer_the_ai_rule(client, session):
    assert 'name="ai_location"' in client.get("/settings").text
    client.post("/settings", data={"ai_location": "on", "days": "90"})

    assert auto_ignore.load_rules(session).ai_location is True


def test_pipeline_skips_without_ollama(session):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    lines: list[str] = []
    # nothing listens there
    settings = config.Settings(ai_analysis=True, ollama_url="http://127.0.0.1:9")
    pipeline.analyze_descriptions(session, settings, lines.append)
    assert lines[0].startswith("ai: skipped: Ollama is not reachable")


def test_pipeline_reports(session, tmp_path):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    add_vacancy(session)
    lines: list[str] = []
    missing = tmp_path / "missing.yaml"
    settings = config.Settings(ai_analysis=True, profile=missing)
    pipeline.analyze_descriptions(session, settings, lines.append, FakeModel([UK_ANSWER]))
    assert lines == [
        f"ai: no {missing}; the stack check is skipped (see profile.example.yaml).",
        "ai: 1 descriptions analyzed, 1 flagged",
    ]


@pytest.mark.parametrize(
    ("places", "quote", "text"),
    [
        (["Russia", "Belarus"], "except Russia and Belarus", "Remote, except Russia and Belarus."),
        (
            ["Poland"],
            "with a preference for candidates based in Poland",
            "Remote, with a preference for candidates based in Poland.",
        ),
        (
            ["\U0001f1ec\U0001f1e7"],
            "\U0001f1ec\U0001f1e7 English B2+",
            "100% Remote | \U0001f1ec\U0001f1e7 English B2+",
        ),
    ],
)
def test_verify_drops_exclusions_preferences_and_flags(places, quote, text):
    assert ai_analysis.verify(answer(residence=places, residence_quote=quote), text).residence == []


def test_any_city_is_not_a_restriction():
    analysis = models.DescriptionAnalysis(work_mode="remote", residence=["любой город"])
    assert ai_analysis.risks(analysis) == []
