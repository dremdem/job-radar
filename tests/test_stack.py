from typing import Any

import pytest
from test_ai_analysis import UK_ANSWER, add_vacancy

from job_radar.ai.analysis import analyze_pending, check_stack, stack_risks
from job_radar.models import DescriptionAnalysis
from job_radar.stack import known_alternative, load_known, says_optional, unknown_mentions

KNOWN = {"Python", "TypeScript", "Kafka", "Docker"}
GO_JOB = (
    "We build ad tech.\n"
    "Requirements:\n"
    "- 5+ years of backend experience, with at least 1 year working in Go in production.\n"
    "- You are strong in at least one of Kotlin, Rust or TypeScript.\n"
    "Nice to have: Prometheus.\n"
    "Let's go! Our Docker images ship daily."
)


class FakeModel:
    """Answers stack questions by skill name; the first call is the work-mode prompt."""

    model = "fake:1b"

    def __init__(self, stack: dict[str, dict[str, str]], facts: dict[str, Any] | None = None):
        self.stack = stack
        self.facts = facts
        self.asked: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "status" not in schema["properties"]:
            return self.facts or {"work_mode": "remote", "work_mode_quote": "", "residence": []}
        skill = prompt.split("does this job REQUIRE ", 1)[1].split(" (", 1)[0]
        self.asked.append(skill)
        return self.stack.get(skill, {"status": "not_needed", "quote": ""})


def test_load_known(tmp_path):
    path = tmp_path / "profile.yaml"
    assert load_known(path) is None
    path.write_text("known:\n  - python\n  - Go\n")
    assert load_known(path) == {"python", "go"}


def test_unknown_mentions_respects_case_and_profile():
    names = [t.name for t in unknown_mentions(GO_JOB, {k.lower() for k in KNOWN})]
    # "go!" as a verb and the known Docker / TypeScript do not count.
    assert names == ["Go", "Kotlin", "Rust", "Prometheus"]
    assert [t.name for t in unknown_mentions("We use golang.", set())] == ["Go"]


def test_check_stack_keeps_required_gaps_only():
    model = FakeModel(
        {
            "Go": {"status": "required", "quote": "at least 1 year working in Go in production"},
            "Kotlin": {
                "status": "required",
                "quote": "strong in at least one of Kotlin, Rust or TypeScript",
            },
            "Rust": {"status": "required", "quote": "Rust experience is a must"},  # invented
            "Prometheus": {"status": "nice_to_have", "quote": "Nice to have: Prometheus."},
        }
    )
    known = {k.lower() for k in KNOWN}
    stack, raw = check_stack(GO_JOB, model, known)
    assert model.asked == ["Go", "Kotlin", "Rust", "Prometheus"]
    by_skill = {s["skill"]: s["status"] for s in stack}
    assert by_skill == {
        "Go": "required",
        "Kotlin": "alternative",  # TypeScript is known
        "Rust": "not_needed",  # the quote is not in the text
        "Prometheus": "nice_to_have",
    }
    assert set(raw) == {"Go", "Kotlin", "Rust", "Prometheus"}


def test_stack_risks():
    analysis = DescriptionAnalysis(
        stack=[
            {"skill": s, "status": "required", "quote": f"{s} in production"}
            for s in ("Go", "gRPC", "Prometheus", "Rust")
        ]
        + [{"skill": "Scala", "status": "nice_to_have", "quote": "Scala"}]
    )
    found = stack_risks(analysis)
    assert found[0][1].text == "Requires Go (not in your CV) — “Go in production”"
    assert len(found) == 4
    assert sum(points for points, _ in found) == -3.0  # capped
    assert stack_risks(DescriptionAnalysis(stack=None)) == []


def test_analyze_pending_checks_stack_also_for_analyzed_descriptions(session):
    o = add_vacancy(session, title="Python Developer", text=GO_JOB)
    # Analyzed before a profile existed: no stack yet.
    from test_ai_analysis import FakeModel as FactsModel

    analyze_pending(session, FactsModel([UK_ANSWER]))
    assert o.analyses[0].stack is None

    model = FakeModel({"Go": {"status": "required", "quote": "working in Go in production"}})
    stats = analyze_pending(session, model, known={k.lower() for k in KNOWN})
    session.refresh(o)
    assert len(o.analyses) == 1  # the same row, now with the stack
    [go] = [s for s in o.analyses[0].stack if s["status"] == "required"]
    assert go["skill"] == "Go"
    assert stats.analyzed == 1 and stats.flagged == 1


def test_queue_shows_stack_gap(client, session):
    add_vacancy(session, title="Python Developer", text=GO_JOB)
    model = FakeModel({"Go": {"status": "required", "quote": "working in Go in production"}})
    analyze_pending(session, model, known={k.lower() for k in KNOWN})
    text = client.get("/").text
    assert "Requires Go (not in your CV) — “working in Go in production”" in text
    assert ">7.0<" in text  # 8.0 - 1


@pytest.mark.parametrize(
    "quote",
    [
        "Strong proficiency in at least one server-side language (Python, Go, Java, Node.js)",
        "Уверенное владение HTML, CSS, JavaScript, PHP или Python",
        "Hands-on experience with cloud platforms (AWS or Azure).",
    ],
)
def test_known_alternative(quote):
    assert known_alternative(quote, {"python", "javascript", "aws"})


def test_slash_counts_only_next_to_a_known_name():
    assert known_alternative("MLOps, AWS/GCP/Azure", {"aws", "gcp"})
    assert not known_alternative("Azure, Docker, Kubernetes, GitOps, CI/CD", {"docker"})


def test_no_alternative_without_a_choice():
    assert not known_alternative("Strong Go and Python skills", {"python"})
    assert not known_alternative("PyTorch или TensorFlow", {"python"})


def test_optional_wording_beats_required():
    assert says_optional("Будет плюсом: Опыт работы с OpenTelemetry")
    assert says_optional("Kafka is a plus")
    assert not says_optional("5+ years of Go in production")
