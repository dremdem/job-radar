from job_radar.ai.analysis import stack_risks
from job_radar.models import DescriptionAnalysis
from job_radar.stack import Profile, load_profile, save_profile, unknown_mentions


def required(*skills):
    return DescriptionAnalysis(
        stack=[{"skill": s, "status": "required", "quote": f"{s} in production"} for s in skills]
    )


def test_profile_round_trip(tmp_path):
    path = tmp_path / "profile.yaml"
    assert load_profile(path) is None
    profile = Profile(
        known=["Python", "Django"],
        learnable=["RabbitMQ"],
        blockers=["TensorFlow"],
        rules=["No relocation."],
        home="Thailand",
        home_regions=["Asia", "APAC"],
    )
    save_profile(path, profile)
    assert "\nhome: Thailand\nknown:" in path.read_text()  # home first, it is short
    assert load_profile(path) == profile


def test_old_profile_with_known_only(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("known:\n  - Python\n")
    assert load_profile(path) == Profile(known=["Python"])


def test_levels():
    p = Profile(known=["Python"], learnable=["RabbitMQ"], blockers=["TensorFlow"])
    assert [p.level(s) for s in ("python", "RabbitMQ", "tensorflow", "Go")] == [
        "known",
        "learnable",
        "blocker",
        "unknown",
    ]


def test_stack_risks_by_level():
    p = Profile(known=["Redis"], learnable=["RabbitMQ"], blockers=["TensorFlow"])
    found = stack_risks(required("Go", "RabbitMQ", "TensorFlow", "Redis"), p)
    texts = [r.text for _, r in found]
    assert texts == [
        "Requires Go (not in your CV) — “Go in production”",
        "Requires RabbitMQ (learnable) — “RabbitMQ in production”",
        "Requires TensorFlow (blocker) — “TensorFlow in production”",
    ]  # Redis is known now: no gap
    assert [points for points, _ in found] == [-1.0, -0.5, -3.0]


def test_non_blockers_are_capped_blockers_are_not():
    p = Profile(blockers=["TensorFlow", "PyTorch"])
    found = stack_risks(required("Go", "Rust", "Scala", "Kotlin", "TensorFlow", "PyTorch"), p)
    assert sum(points for points, _ in found) == -9.0  # -3 capped + -3 - 3


def test_profile_items_outside_the_catalogue_are_looked_for():
    p = Profile(blockers=["Haskell"])
    names = [t.name for t in unknown_mentions("Strong Haskell skills.", set(), p.extra_techs())]
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
    saved = load_profile(client.app.state.settings.profile)
    assert saved == Profile(
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
    from test_ai_analysis import UK_ANSWER, FakeModel, add_vacancy

    from job_radar.ai.analysis import analyze_pending

    o = add_vacancy(session)
    analyze_pending(session, FakeModel([UK_ANSWER]), known=set())
    o.analyses[0].stack = []
    session.commit()
    client.post(
        "/profile", data={"known": "Python", "learnable": "", "blockers": "Go", "rules": ""}
    )
    session.expire_all()
    assert session.get(DescriptionAnalysis, o.analyses[0].id).stack is None


def test_header_links_profile(client):
    assert 'href="/profile"' in client.get("/").text


def test_home_from_the_profile_page_reaches_the_score(client):
    from job_radar.scoring import score_job

    assert "Thailand listed" not in [r.text for r in score_job("Dev", "Thailand").reasons]
    client.post("/profile", data={"known": "Python", "home": "Thailand"})
    assert "Thailand listed" in [r.text for r in score_job("Dev", "Thailand").reasons]
