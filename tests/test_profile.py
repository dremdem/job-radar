"""Tests for the stack profile: reading and saving profile.yaml, technology levels
and the risks they give (docs/ai.md#stack-vs-your-cv).
"""

import job_radar.ai.analysis as ai_analysis
import job_radar.findings as findings
import job_radar.models as models
import job_radar.stack as stack


def required(*skills):
    return models.DescriptionAnalysis(
        stack=[{"skill": s, "status": "required", "quote": f"{s} in production"} for s in skills]
    )


def test_profile_round_trip(tmp_path):
    path = tmp_path / "profile.yaml"
    assert stack.load_profile(path) is None
    profile = stack.Profile(
        known=["Python", "Django"],
        learnable=["RabbitMQ"],
        blockers=["TensorFlow"],
        rules=["No relocation."],
        home="Thailand",
        home_regions=["Asia", "APAC"],
    )
    stack.save_profile(path, profile)
    # home first, it is short
    assert "\nhome: Thailand\nknown:" in path.read_text()
    assert stack.load_profile(path) == profile


def test_old_profile_with_known_only(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("known:\n  - Python\n")
    assert stack.load_profile(path) == stack.Profile(known=["Python"])


def test_levels():
    p = stack.Profile(known=["Python"], learnable=["RabbitMQ"], blockers=["TensorFlow"])
    assert [p.level(s) for s in ("python", "RabbitMQ", "tensorflow", "Go")] == [
        "known",
        "learnable",
        "blocker",
        "unknown",
    ]


def test_stack_risks_by_level():
    p = stack.Profile(known=["Redis"], learnable=["RabbitMQ"], blockers=["TensorFlow"])
    found = findings.stack_risks(required("Go", "RabbitMQ", "TensorFlow", "Redis"), p)
    texts = [r.text for _, r in found]
    assert texts == [
        "Requires Go (not in your CV) — “Go in production”",
        "Requires RabbitMQ (learnable) — “RabbitMQ in production”",
        "Requires TensorFlow (blocker) — “TensorFlow in production”",
        # Redis is known now: no gap
    ]
    assert [points for points, _ in found] == [-1.0, -0.5, -3.0]


def test_non_blockers_are_capped_blockers_are_not():
    p = stack.Profile(blockers=["TensorFlow", "PyTorch"])
    found = findings.stack_risks(
        required("Go", "Rust", "Scala", "Kotlin", "TensorFlow", "PyTorch"), p
    )
    # -3 capped + -3 - 3
    assert sum(points for points, _ in found) == -9.0


def test_profile_items_outside_the_catalogue_are_looked_for():
    p = stack.Profile(blockers=["Haskell"])
    names = [
        t.name for t in stack.unknown_mentions("Strong Haskell skills.", set(), p.extra_techs())
    ]
    assert names == ["Haskell"]


def test_profile_page(client, tmp_path, monkeypatch):
    text = client.get("/profile").text
    assert 'name="known"' in text and 'name="blockers"' in text and 'name="rules"' in text
    response = client.post(
        "/profile",
        data={
            "known": "Python\n Django \n\n",
            "learnable": "RabbitMQ",
            "blockers": "TensorFlow",
            "rules": "No relocation.",
            "home": " Thailand ",
            "home_regions": "Asia\nAPAC",
        },
    )
    assert response.status_code == 200 and "Saved" in response.text
    saved = stack.load_profile(client.app.state.settings.profile)
    assert saved == stack.Profile(
        known=["Python", "Django"],
        learnable=["RabbitMQ"],
        blockers=["TensorFlow"],
        rules=["No relocation."],
        home="Thailand",
        home_regions=["Asia", "APAC"],
    )
    page = client.get("/profile").text
    assert "TensorFlow" in page and 'value="Thailand"' in page


def test_saving_the_profile_asks_the_stack_again(client, session):
    import test_ai_analysis

    o = test_ai_analysis.add_vacancy(session)
    ai_analysis.analyze_pending(
        session, test_ai_analysis.FakeModel([test_ai_analysis.UK_ANSWER]), known=set()
    )
    o.analyses[0].stack = []
    session.commit()
    client.post(
        "/profile", data={"known": "Python", "learnable": "", "blockers": "Go", "rules": ""}
    )
    session.expire_all()
    assert session.get(models.DescriptionAnalysis, o.analyses[0].id).stack is None


def test_header_links_profile(client):
    assert 'href="/profile"' in client.get("/").text


def test_home_from_the_profile_page_reaches_the_score(client):
    import job_radar.scoring as scoring

    assert "Thailand listed" not in [r.text for r in scoring.score_job("Dev", "Thailand").reasons]
    client.post("/profile", data={"known": "Python", "home": "Thailand"})
    assert "Thailand listed" in [r.text for r in scoring.score_job("Dev", "Thailand").reasons]
