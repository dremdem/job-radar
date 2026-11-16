"""Tests for job_radar.suggestions: ready-made ignore reasons from the checks and the
one-tap ignore in the queue (docs/applications.md#reasons-and-notes).
"""

import test_ai_analysis

import job_radar.models as models
import job_radar.profile.stack as profile_stack
import job_radar.suggestions as suggestions


def kinds(found):
    return [(s.kind, s.detail, s.label) for s in found]


def test_language_suggestion(session):
    o = test_ai_analysis.add_vacancy(
        session, title="Senior Python Developer", text="Fluent German is required."
    )
    assert kinds(suggestions.suggest(o, description="Fluent German is required.")) == [
        (models.NoteKind.LANGUAGE, "German", "language: German")
    ]


def test_local_model_suggestions():
    analysis = models.DescriptionAnalysis(
        work_mode="hybrid",
        work_mode_quote="3 days in office",
        residence=["EU"],
        residence_quote="EU only",
        stack=[{"skill": "TensorFlow", "status": "required", "quote": "x"}],
    )
    found = suggestions.suggest(
        None,
        description="",
        analysis=analysis,
        profile=profile_stack.Profile(blockers=["TensorFlow"]),
    )
    assert kinds(found) == [
        (models.NoteKind.ONLY_LOCATION, "EU", "only EU"),
        (models.NoteKind.WORK_FORMAT, None, "hybrid / on-site"),
        (models.NoteKind.STACK, "TensorFlow", "stack: TensorFlow"),
    ]


def test_claude_skip_suggestion_prefers_its_blocker():
    def claude(result):
        return models.ClaudeAnalysis(state="done", result=result)

    gap_language = {
        "verdict": "skip",
        "summary": "No.",
        "location": {"verdict": "ok"},
        "gaps": [{"skill": "Нидерландский или французский (свободно)", "blocker": True}],
    }
    assert kinds(suggestions.suggest(None, description="", claude=claude(gap_language)))[0][:2] == (
        models.NoteKind.LANGUAGE,
        "Dutch",
    )
    gap_stack = {
        "verdict": "skip",
        "summary": "No.",
        "location": {"verdict": "ok"},
        "gaps": [{"skill": "Azure", "blocker": True}],
    }
    assert kinds(suggestions.suggest(None, description="", claude=claude(gap_stack))) == [
        (models.NoteKind.STACK, "Azure", "stack: Azure")
    ]
    location = {
        "verdict": "skip",
        "summary": "Office in Limassol.",
        "location": {"verdict": "blocker", "note": "Only office in Limassol"},
        "gaps": [],
    }
    assert (
        kinds(suggestions.suggest(None, description="", claude=claude(location)))[0][0]
        is models.NoteKind.OTHER
    )
    plain = {"verdict": "skip", "summary": "Infra role, not Python. More text.", "gaps": []}
    [s] = suggestions.suggest(None, description="", claude=claude(plain))
    assert s.kind is models.NoteKind.OTHER and s.comment == "Claude: Infra role, not Python."
    assert suggestions.suggest(None, description="", claude=claude({"verdict": "apply"})) == []


def test_duplicates_are_dropped_and_at_most_three():
    analysis = models.DescriptionAnalysis(
        work_mode="onsite", work_mode_quote="office", residence=["UK"], residence_quote="UK only"
    )
    claude = models.ClaudeAnalysis(
        state="done",
        result={
            "verdict": "skip",
            "summary": "UK only.",
            "gaps": [{"skill": "Go", "blocker": True}, {"skill": "Rust", "blocker": True}],
        },
    )
    found = suggestions.suggest(
        None, description="Fluent German is required.", analysis=analysis, claude=claude
    )
    assert len(found) == 3
    assert len({(s.kind, s.detail) for s in found}) == 3


def test_one_tap_ignore_from_the_row(client, session):
    o = test_ai_analysis.add_vacancy(
        session, title="Senior Python Developer", text="Fluent German is required."
    )
    assert "ignore? \U0001f5e3\ufe0f language: German" in client.get("/").text
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
    assert o.state is models.OpportunityState.IGNORE
    assert "language: German" in client.get(f"/opportunities/{o.id}").text


def test_reason_chips_and_work_format_reason(client, session):
    o = test_ai_analysis.add_vacancy(session, title="Python Developer", text="Remote.")
    o.location = "Lucknow, India"
    session.commit()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert 'class="chip"' in panel and ">\u2708\ufe0f Location: India<" in panel
    assert ">\U0001f393 Seniority<" in panel
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
    [s] = suggestions.suggest(
        None, description="", claude=models.ClaudeAnalysis(state="done", result=result)
    )
    assert (s.kind, s.detail, s.label) == (models.NoteKind.ONLY_LOCATION, "EU only", "only EU only")
    result["ignore_reason"] = {"kind": "seniority", "detail": "team lead"}
    [s] = suggestions.suggest(
        None, description="", claude=models.ClaudeAnalysis(state="done", result=result)
    )
    assert (s.kind, s.label, s.comment) == (
        models.NoteKind.SENIORITY,
        "seniority: team lead",
        "Claude: team lead",
    )


def test_old_long_gaps_are_shortened():
    result = {
        "verdict": "skip",
        "summary": "No.",
        "gaps": [{"skill": "Профессиональный опыт в DevOps-роли (5+ лет)", "blocker": True}],
    }
    [s] = suggestions.suggest(
        None, description="", claude=models.ClaudeAnalysis(state="done", result=result)
    )
    assert s.label == "stack: Профессиональный опыт в…"


def test_one_language_suggestion_even_from_two_sources():
    result = {
        "verdict": "skip",
        "summary": "No.",
        "ignore_reason": {"kind": "language", "detail": "Dutch/French"},
    }
    found = suggestions.suggest(
        None,
        description="Speak fluent Dutch and/or French.",
        claude=models.ClaudeAnalysis(state="done", result=result),
    )
    assert [s.label for s in found] == ["language: Dutch"]


def test_reasons_have_icons(client, session):
    import job_radar.notes as notes

    # every reason has a picture (#195)
    assert set(notes.REASON_ICONS) == set(models.NoteKind)
    assert notes.reason_icon("salary") == "\U0001f4b0" and notes.reason_icon("nope") == ""
    o = test_ai_analysis.add_vacancy(session, title="Python Developer", text="Remote.")
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "\U0001f4b0 Salary<" in panel and "\U0001f3e2 Hybrid / on-site<" in panel
    client.post(f"/opportunities/{o.id}/notes", data={"action": "ignore", "kind": "salary"})
    assert "\U0001f4b0 <b>ignored</b>" in client.get(f"/opportunities/{o.id}").text
