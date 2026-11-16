"""Tests for job_radar.profile.stack and the local stack check: technologies a description
mentions that the profile lacks (docs/ai.md#stack-vs-your-cv).
"""

import typing as t

import pytest
import test_ai_analysis

import job_radar.ai.local.analysis as local_analysis
import job_radar.models as models
import job_radar.profile.stack as profile_stack
import job_radar.scoring.findings as scoring_findings

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

    def __init__(self, stack: dict[str, dict[str, str]], facts: dict[str, t.Any] | None = None):
        self.stack = stack
        self.facts = facts
        self.asked: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, t.Any]) -> dict[str, t.Any]:
        if "status" not in schema["properties"]:
            return self.facts or {"work_mode": "remote", "work_mode_quote": "", "residence": []}
        skill = prompt.split("does this job REQUIRE ", 1)[1].split(" (", 1)[0]
        self.asked.append(skill)
        return self.stack.get(skill, {"status": "not_needed", "quote": ""})


def test_load_known(tmp_path):
    path = tmp_path / "profile.yaml"
    assert profile_stack.load_known(path) is None
    path.write_text("known:\n  - python\n  - Go\n")
    assert profile_stack.load_known(path) == {"python", "go"}


def test_unknown_mentions_respects_case_and_profile():
    names = [
        tech.name for tech in profile_stack.unknown_mentions(GO_JOB, {k.lower() for k in KNOWN})
    ]
    # "go!" as a verb and the known Docker / TypeScript do not count.
    assert names == ["Go", "Kotlin", "Rust", "Prometheus"]
    assert [tech.name for tech in profile_stack.unknown_mentions("We use golang.", set())] == ["Go"]


def test_check_stack_keeps_required_gaps_only():
    model = FakeModel(
        {
            "Go": {"status": "required", "quote": "at least 1 year working in Go in production"},
            "Kotlin": {
                "status": "required",
                "quote": "strong in at least one of Kotlin, Rust or TypeScript",
            },
            # invented
            "Rust": {"status": "required", "quote": "Rust experience is a must"},
            "Prometheus": {"status": "nice_to_have", "quote": "Nice to have: Prometheus."},
        }
    )
    known = {k.lower() for k in KNOWN}
    stack, raw = local_analysis.check_stack(GO_JOB, model, known)
    assert model.asked == ["Go", "Kotlin", "Rust", "Prometheus"]
    by_skill = {s["skill"]: s["status"] for s in stack}
    assert by_skill == {
        "Go": "required",
        # TypeScript is known
        "Kotlin": "alternative",
        # the quote is not in the text
        "Rust": "not_needed",
        "Prometheus": "nice_to_have",
    }
    assert set(raw) == {"Go", "Kotlin", "Rust", "Prometheus"}


def test_stack_risks():
    analysis = models.DescriptionAnalysis(
        stack=[
            {"skill": s, "status": "required", "quote": f"{s} in production"}
            for s in ("Go", "gRPC", "Prometheus", "Rust")
        ]
        + [{"skill": "Scala", "status": "nice_to_have", "quote": "Scala"}]
    )
    found = scoring_findings.stack_risks(analysis)
    assert found[0][1].text == "Requires Go (not in your CV) — “Go in production”"
    assert len(found) == 4
    # capped
    assert sum(points for points, _ in found) == -3.0
    assert scoring_findings.stack_risks(models.DescriptionAnalysis(stack=None)) == []


def test_analyze_pending_checks_stack_also_for_analyzed_descriptions(session):
    o = test_ai_analysis.add_vacancy(session, title="Python Developer", text=GO_JOB)
    # Analyzed before a profile existed: no stack yet.

    local_analysis.analyze_pending(
        session, test_ai_analysis.FakeModel([test_ai_analysis.UK_ANSWER])
    )
    assert o.analyses[0].stack is None

    model = FakeModel({"Go": {"status": "required", "quote": "working in Go in production"}})
    stats = local_analysis.analyze_pending(session, model, known={k.lower() for k in KNOWN})
    session.refresh(o)
    # the same row, now with the stack
    assert len(o.analyses) == 1
    [go] = [s for s in o.analyses[0].stack if s["status"] == "required"]
    assert go["skill"] == "Go"
    assert stats.analyzed == 1 and stats.flagged == 1


def test_queue_shows_stack_gap(client, session):
    o = test_ai_analysis.add_vacancy(session, title="Python Developer", text=GO_JOB)
    model = FakeModel({"Go": {"status": "required", "quote": "working in Go in production"}})
    local_analysis.analyze_pending(session, model, known={k.lower() for k in KNOWN})
    # 8.0 - 1
    assert ">7.0<" in client.get("/").text
    text = client.get(f"/opportunities/{o.id}/panel").text
    assert "Requires Go (not in your CV) — “working in Go in production”" in text


@pytest.mark.parametrize(
    "quote",
    [
        "Strong proficiency in at least one server-side language (Python, Go, Java, Node.js)",
        "Уверенное владение HTML, CSS, JavaScript, PHP или Python",
        "Hands-on experience with cloud platforms (AWS or Azure).",
    ],
)
def test_known_alternative(quote):
    assert profile_stack.known_alternative(quote, {"python", "javascript", "aws"})


def test_slash_counts_only_next_to_a_known_name():
    assert profile_stack.known_alternative("MLOps, AWS/GCP/Azure", {"aws", "gcp"})
    assert not profile_stack.known_alternative(
        "Azure, Docker, Kubernetes, GitOps, CI/CD", {"docker"}
    )


def test_no_alternative_without_a_choice():
    assert not profile_stack.known_alternative("Strong Go and Python skills", {"python"})
    assert not profile_stack.known_alternative("PyTorch или TensorFlow", {"python"})


def test_optional_wording_beats_required():
    assert profile_stack.says_optional("Будет плюсом: Опыт работы с OpenTelemetry")
    assert profile_stack.says_optional("Kafka is a plus")
    assert not profile_stack.says_optional("5+ years of Go in production")
