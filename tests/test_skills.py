from datetime import UTC, datetime

import pytest
from test_ai_analysis import add_vacancy

from job_radar.models import ClaudeAnalysis, DescriptionAnalysis, OpportunityState
from job_radar.skills import best_set, build_report, normalize
from job_radar.stack import Profile

PROFILE = Profile(known=["Python", "Django"], learnable=["Redis"])


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("PySpark", "Spark"),
        ("Apache Spark (big data)", "Spark"),
        ("K8s", "Kubernetes"),
        ("Go (5+ years in production)", "Go"),
        ("Terraform / CloudFormation", "Terraform"),
        ("Observability", "Observability"),
        ("Bachelor's degree in CS", None),
        ("10+ лет опыта", None),
        ("Team lead experience", None),
        ("Fluent German", None),
    ],
)
def test_normalize(raw, name):
    assert normalize(raw) == name


def claude(session, o, gaps, location="ok"):
    session.add(
        ClaudeAnalysis(
            opportunity_id=o.id,
            state="done",
            model="sonnet",
            prompt_version="2",
            finished_at=datetime.now(UTC),
            result={"verdict": "maybe", "location": {"verdict": location}, "gaps": gaps},
        )
    )
    session.commit()


def gap(skill, must=True, blocker=False):
    return {"skill": skill, "requirement": "must_have" if must else "nice_to_have",
            "blocker": blocker, "note": "", "quote": f"{skill} in production"}  # fmt: skip


def test_report_counts_and_unlocks(session):
    only_go = add_vacancy(session, title="A", text="Python and Go.")
    claude(session, only_go, [gap("Go")])
    go_and_spark = add_vacancy(session, title="B", text="Python, Go and Spark.")
    claude(session, go_and_spark, [gap("Golang"), gap("PySpark"), gap("dbt", must=False)])
    blocked = add_vacancy(session, title="C", text="Python and Go, UK only.")
    claude(session, blocked, [gap("Go")], location="blocker")
    known = add_vacancy(session, title="D", text="Python.")
    claude(session, known, [gap("Django"), gap("Bachelor's degree", blocker=True)])
    ignored = add_vacancy(session, title="E", text="Go", state=OpportunityState.IGNORE)
    claude(session, ignored, [gap("Go")])  # ignored without a stack reason: left out

    report = build_report(session, PROFILE)
    rows = {r.name: r for r in report.rows}
    assert rows["Go"].must == 3 and rows["Go"].unlock == 1  # only A: B also needs Spark
    assert rows["Spark"].must == 1 and rows["Spark"].unlock == 0
    assert rows["dbt"].nice == 1 and rows["dbt"].must == 0
    assert "Django" not in rows  # known
    assert report.rows[0].name == "Go"
    assert report.non_tech["Bachelor's degree"] == 1
    assert rows["Go"].level == "unknown" and [e.title for e in rows["Go"].examples][:1] == ["A"]


def test_local_stack_check_counts_too(session):
    o = add_vacancy(session, title="Local", text="Terraform required.")
    session.add(DescriptionAnalysis(
        snapshot_id=o.source_records[0].snapshots[0].id, opportunity_id=o.id, model="m",
        work_mode="remote", residence=[],
        stack=[{"skill": "Terraform", "status": "required", "quote": "Terraform required."}],
    ))  # fmt: skip
    session.commit()
    rows = {r.name: r for r in build_report(session, PROFILE).rows}
    assert rows["Terraform"].must == 1 and rows["Terraform"].unlock == 1


def test_best_set_is_greedy():
    missing = [{"Go"}, {"Go"}, {"Go", "Spark"}, {"Spark", "Terraform"}, {"Redis"}]
    assert best_set(missing, 1) == ["Go"]
    assert best_set(missing, 2) == ["Go", "Spark"]  # Go + Spark opens 3
    assert best_set(missing, 3)[:2] == ["Go", "Spark"]


def test_skills_page_and_level_action(client, session):
    o = add_vacancy(session, title="Go job", text="Python and Go.")
    claude(session, o, [gap("Go")])
    page = client.get("/skills").text
    assert ">Go<" in page and "Would unlock" in page and 'href="/skills"' in page
    response = client.post("/skills/level", data={"name": "Go", "level": "learnable"})
    assert response.status_code == 200
    from job_radar.stack import load_profile

    assert "Go" in load_profile(client.app.state.settings.profile).learnable
    client.post("/skills/level", data={"name": "Go", "level": "known"})
    profile = load_profile(client.app.state.settings.profile)
    assert "Go" in profile.known and "Go" not in profile.learnable


def test_all_analysed_counts_ignored_too(session):
    o = add_vacancy(session, title="Ignored", text="Go", state=OpportunityState.IGNORE)
    claude(session, o, [gap("Go")])
    assert "Go" not in {r.name for r in build_report(session, PROFILE).rows}
    assert "Go" in {r.name for r in build_report(session, PROFILE, everything=True).rows}


def test_sort_rows():
    from job_radar.skills import SkillRow, sort_rows

    rows = [SkillRow("Go", must=3, nice=1, unlock=1, scores=[6.0]),
            SkillRow("Azure", must=5, nice=4, unlock=0, scores=[8.0]),
            SkillRow("Redis", must=1, nice=0, unlock=2, level="learnable")]  # fmt: skip
    assert [r.name for r in sort_rows(rows, "must", "desc")] == ["Azure", "Go", "Redis"]
    assert [r.name for r in sort_rows(rows, "name", "asc")] == ["Azure", "Go", "Redis"]
    assert [r.name for r in sort_rows(rows, "avg", "desc")] == [
        "Azure",
        "Go",
        "Redis",
    ]  # no score last
    assert [r.name for r in sort_rows(rows, "unlock", "asc")] == ["Azure", "Go", "Redis"]
    assert sort_rows(rows, "nope", "desc")[0].name == "Redis"  # default: unlock


def test_sort_links_on_the_page(client, session):
    o = add_vacancy(session, title="Go job", text="Python and Go.")
    claude(session, o, [gap("Go")])
    page = client.get("/skills?sort=must&dir=desc&scope=all").text
    assert 'href="/skills?sort=must&dir=asc&days=0&scope=all"' in page  # flips
    assert 'href="/skills?sort=nice&dir=desc&days=0&scope=all"' in page
    assert "▼" in page
