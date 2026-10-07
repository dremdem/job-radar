from test_ai_analysis import add_vacancy

from job_radar.models import ClaudeAnalysis, DescriptionAnalysis, NoteKind, OpportunityState
from job_radar.stack import Profile
from job_radar.suggestions import suggest


def kinds(found):
    return [(s.kind, s.detail, s.label) for s in found]


def test_language_suggestion(session):
    o = add_vacancy(session, title="Senior Python Developer", text="Fluent German is required.")
    assert kinds(suggest(o, description="Fluent German is required.")) == [
        (NoteKind.LANGUAGE, "German", "language: German")
    ]


def test_local_model_suggestions():
    analysis = DescriptionAnalysis(
        work_mode="hybrid",
        work_mode_quote="3 days in office",
        residence=["EU"],
        residence_quote="EU only",
        stack=[{"skill": "TensorFlow", "status": "required", "quote": "x"}],
    )
    found = suggest(
        None, description="", analysis=analysis, profile=Profile(blockers=["TensorFlow"])
    )
    assert kinds(found) == [
        (NoteKind.ONLY_LOCATION, "EU", "only EU"),
        (NoteKind.WORK_FORMAT, None, "hybrid / on-site"),
        (NoteKind.STACK, "TensorFlow", "stack: TensorFlow"),
    ]


def test_claude_skip_suggestion_prefers_its_blocker():
    def claude(result):
        return ClaudeAnalysis(state="done", result=result)

    gap_language = {
        "verdict": "skip",
        "summary": "No.",
        "location": {"verdict": "ok"},
        "gaps": [{"skill": "Нидерландский или французский (свободно)", "blocker": True}],
    }
    assert kinds(suggest(None, description="", claude=claude(gap_language)))[0][:2] == (
        NoteKind.LANGUAGE,
        "Dutch",
    )
    gap_stack = {
        "verdict": "skip",
        "summary": "No.",
        "location": {"verdict": "ok"},
        "gaps": [{"skill": "Azure", "blocker": True}],
    }
    assert kinds(suggest(None, description="", claude=claude(gap_stack))) == [
        (NoteKind.STACK, "Azure", "stack: Azure")
    ]
    location = {
        "verdict": "skip",
        "summary": "Office in Limassol.",
        "location": {"verdict": "blocker", "note": "Only office in Limassol"},
        "gaps": [],
    }
    assert kinds(suggest(None, description="", claude=claude(location)))[0][0] is NoteKind.OTHER
    plain = {"verdict": "skip", "summary": "Infra role, not Python. More text.", "gaps": []}
    [s] = suggest(None, description="", claude=claude(plain))
    assert s.kind is NoteKind.OTHER and s.comment == "Claude: Infra role, not Python."
    assert suggest(None, description="", claude=claude({"verdict": "apply"})) == []


def test_duplicates_are_dropped_and_at_most_three():
    analysis = DescriptionAnalysis(
        work_mode="onsite", work_mode_quote="office", residence=["UK"], residence_quote="UK only"
    )
    claude = ClaudeAnalysis(
        state="done",
        result={
            "verdict": "skip",
            "summary": "UK only.",
            "gaps": [{"skill": "Go", "blocker": True}, {"skill": "Rust", "blocker": True}],
        },
    )
    found = suggest(
        None, description="Fluent German is required.", analysis=analysis, claude=claude
    )
    assert len(found) == 3
    assert len({(s.kind, s.detail) for s in found}) == 3


def test_one_tap_ignore_from_the_row(client, session):
    o = add_vacancy(session, title="Senior Python Developer", text="Fluent German is required.")
    assert "ignore? language: German" in client.get("/").text
    assert "Ignore: language: German" in client.get(f"/opportunities/{o.id}/panel").text
    response = client.post(
        f"/opportunities/{o.id}/notes",
        data={
            "action": "ignore",
            "kind": "language",
            "detail": "German",
            "comment": "",
            "from": "panel",
        },
    )
    assert response.status_code == 200
    session.refresh(o)
    assert o.state is OpportunityState.IGNORE
    assert "language: German" in client.get(f"/opportunities/{o.id}").text


def test_reason_chips_and_work_format_reason(client, session):
    o = add_vacancy(session, title="Python Developer", text="Remote.")
    o.location = "Lucknow, India"
    session.commit()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert 'class="chip"' in panel and ">Location: India<" in panel and ">Seniority<" in panel
    client.post(f"/opportunities/{o.id}/notes", data={"action": "ignore", "kind": "work_format"})
    assert "hybrid / on-site" in client.get(f"/opportunities/{o.id}").text
    client.post(
        f"/opportunities/{o.id}/notes",
        data={"action": "ignore", "kind": "stack", "detail": "Azure"},
    )
    assert "stack: Azure" in client.get(f"/opportunities/{o.id}").text


def test_claude_names_its_ignore_reason():
    result = {
        "verdict": "skip",
        "summary": "No.",
        "gaps": [{"skill": "Long text", "blocker": True}],
        "ignore_reason": {"kind": "location", "detail": "EU only"},
    }
    [s] = suggest(None, description="", claude=ClaudeAnalysis(state="done", result=result))
    assert (s.kind, s.detail, s.label) == (NoteKind.ONLY_LOCATION, "EU only", "only EU only")
    result["ignore_reason"] = {"kind": "seniority", "detail": "team lead"}
    [s] = suggest(None, description="", claude=ClaudeAnalysis(state="done", result=result))
    assert (s.kind, s.label, s.comment) == (
        NoteKind.SENIORITY,
        "seniority: team lead",
        "Claude: team lead",
    )


def test_old_long_gaps_are_shortened():
    result = {
        "verdict": "skip",
        "summary": "No.",
        "gaps": [{"skill": "Профессиональный опыт в DevOps-роли (5+ лет)", "blocker": True}],
    }
    [s] = suggest(None, description="", claude=ClaudeAnalysis(state="done", result=result))
    assert s.label == "stack: Профессиональный опыт в…"


def test_one_language_suggestion_even_from_two_sources():
    result = {
        "verdict": "skip",
        "summary": "No.",
        "ignore_reason": {"kind": "language", "detail": "Dutch/French"},
    }
    found = suggest(
        None,
        description="Speak fluent Dutch and/or French.",
        claude=ClaudeAnalysis(state="done", result=result),
    )
    assert [s.label for s in found] == ["language: Dutch"]
