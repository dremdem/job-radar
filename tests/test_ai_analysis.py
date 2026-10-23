from typing import Any

import pytest

from job_radar.ai.analysis import analyze_pending, quote_in_text, risks, verify
from job_radar.auto_ignore import Rules, save_rules
from job_radar.dedup import add_snapshot, get_or_create_company, upsert_source_record
from job_radar.models import DescriptionAnalysis, Opportunity, OpportunityState, SourceType

UK_ONLY = (
    "We build payments.\nThe applicant must be based in the UK and able to work fully remotely.\n"
    "Python and FastAPI."
)


class FakeModel:
    model = "fake:1b"

    def __init__(self, answers: list[dict[str, Any] | Exception]):
        self.answers = answers
        self.prompts: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(mode="remote", mode_quote="", residence=(), residence_quote="") -> dict[str, Any]:
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


def add_vacancy(session, title="Python Developer", text=UK_ONLY, state=OpportunityState.NEW):
    o = Opportunity(title=title, company=get_or_create_company(session, "Acme"), score=8.0)
    o.state = state
    session.add(o)
    session.flush()
    record = upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    if text:
        add_snapshot(session, record, text)
    session.commit()
    return o


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("The applicant must be based in the UK", True),
        ("the applicant  MUST be based\nin the UK.", True),  # case, spacing, final dot
        ("The applicant must be based in UK and able to work fully remotely", True),  # slip
        ("Candidates must reside in Germany.", False),  # invented
        ("", False),
    ],
)
def test_quote_in_text(quote, found):
    assert quote_in_text(quote, UK_ONLY) is found


def test_verify_keeps_supported_facts():
    facts = verify(UK_ANSWER, UK_ONLY)
    assert facts.residence == ["UK"] and facts.work_mode == "remote"


def test_verify_drops_invented_quotes_and_places():
    facts = verify(
        answer("hybrid", "Three days a week in our Berlin office.", ["UK", "Thailand"], "x y z"),
        UK_ONLY,
    )
    assert facts.work_mode == "unknown" and facts.work_mode_quote == ""
    assert facts.residence == []
    # A place the text never names is dropped even with a real quote.
    facts = verify(
        answer(residence=["Thailand"], residence_quote=UK_ANSWER["residence_quote"]), UK_ONLY
    )
    assert facts.residence == []


def test_risks():
    hybrid = DescriptionAnalysis(
        work_mode="hybrid", work_mode_quote="2 days in office", residence=[]
    )
    [(points, reason)] = risks(hybrid)
    assert points == -3.0 and reason.text == "AI: hybrid — “2 days in office”"

    uk = DescriptionAnalysis(
        work_mode="remote", residence=["UK"], residence_quote="based in the UK"
    )
    [(points, reason)] = risks(uk)
    assert points == -2.5 and reason.text == "AI: only for UK — “based in the UK”"

    for open_place in ("Worldwide", "Thailand", "APAC", "Asia"):
        assert risks(DescriptionAnalysis(work_mode="remote", residence=[open_place])) == []


def test_analyze_pending_stores_once_and_only_for_to_review(session):
    flagged = add_vacancy(session)
    add_vacancy(session, title="No description", text=None)
    add_vacancy(session, title="Mine", state=OpportunityState.INTERESTED)

    model = FakeModel([UK_ANSWER])
    stats = analyze_pending(session, model)
    assert (stats.analyzed, stats.flagged, stats.errors) == (1, 1, 0)
    [analysis] = session.query(DescriptionAnalysis).all()
    assert analysis.opportunity_id == flagged.id and analysis.model == "fake:1b"
    assert analysis.residence == ["UK"] and analysis.raw == UK_ANSWER
    assert "must be based in the UK" in model.prompts[0]

    # Already analyzed: nothing to do.
    assert analyze_pending(session, FakeModel([])).analyzed == 0


def test_analyze_pending_limit_and_errors(session):
    for title in ("A", "B", "C"):
        add_vacancy(session, title=title, text=f"{title}: {UK_ONLY}")
    stats = analyze_pending(session, FakeModel([RuntimeError("bad JSON"), UK_ANSWER]), limit=2)
    assert (stats.analyzed, stats.errors, stats.pending) == (1, 1, 1)
    assert stats.last_error == "bad JSON"


def test_ai_rule_auto_ignores(session):
    save_rules(session, Rules(ai_location=True))
    o = add_vacancy(session)
    analyze_pending(session, FakeModel([UK_ANSWER]))
    session.refresh(o)
    assert o.state is OpportunityState.AUTO_IGNORE
    assert o.auto_ignore_reason == "AI: only for UK"


def test_ai_rule_is_off_by_default(session):
    o = add_vacancy(session)
    analyze_pending(session, FakeModel([UK_ANSWER]))
    session.refresh(o)
    assert o.state is OpportunityState.NEW


def test_queue_shows_ai_reason_and_lowers_score(client, session):
    o = add_vacancy(session)
    analyze_pending(session, FakeModel([UK_ANSWER]))
    # 8.0 - 2.5; no Python bonus, the title already says Python.
    assert ">5.5<" in client.get("/").text
    text = client.get(f"/opportunities/{o.id}/panel").text
    assert "AI: only for UK — “The applicant must be based in the UK" in text


def test_settings_offer_the_ai_rule(client, session):
    assert 'name="ai_location"' in client.get("/settings").text
    client.post("/settings", data={"ai_location": "on", "days": "90"})
    from job_radar.auto_ignore import load_rules

    assert load_rules(session).ai_location is True


def test_pipeline_skips_without_ollama(session):
    from job_radar import pipeline
    from job_radar.config import Settings

    lines: list[str] = []
    settings = Settings(ai_analysis=True, ollama_url="http://127.0.0.1:9")  # nothing listens there
    pipeline.analyze_descriptions(session, settings, lines.append)
    assert lines[0].startswith("ai: skipped: Ollama is not reachable")


def test_pipeline_reports(session, tmp_path):
    from job_radar import pipeline
    from job_radar.config import Settings

    add_vacancy(session)
    lines: list[str] = []
    missing = tmp_path / "missing.yaml"
    settings = Settings(ai_analysis=True, profile=missing)
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
    assert verify(answer(residence=places, residence_quote=quote), text).residence == []


def test_any_city_is_not_a_restriction():
    analysis = DescriptionAnalysis(work_mode="remote", residence=["любой город"])
    assert risks(analysis) == []
