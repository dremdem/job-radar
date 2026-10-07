import json
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest

from job_radar.claude import analysis as claude
from job_radar.claude.prompt import FACTS, SCHEMA, analysis_prompt
from job_radar.claude.runner import ClaudeCli, ClaudeError, Reply
from job_radar.models import ClaudeAnalysis
from job_radar.stack import Profile

DESCRIPTION = (
    "We are a fintech startup.\nRequirements:\n- 5+ years of Python and FastAPI.\n"
    "- Professional TensorFlow experience.\nThis is a fully remote role, worldwide."
)
RESULT = {
    "verdict": "maybe",
    "match": 70,
    "summary": "Хороший стек, но нужен TensorFlow.",
    "location": {
        "verdict": "ok",
        "note": "Удалённо по миру",
        "quote": "This is a fully remote role, worldwide.",
    },
    "strengths": [{"point": "Python и FastAPI", "quote": "5+ years of Python and FastAPI."}],
    "gaps": [
        {
            "skill": "TensorFlow",
            "requirement": "must_have",
            "blocker": True,
            "note": "Профессиональный уровень",
            "quote": "Professional TensorFlow experience.",
        }
    ],
    "red_flags": [{"point": "Придуманная цитата", "quote": "Must relocate to Berlin."}],
    "questions": ["Насколько критичен TensorFlow?"],
    "cv_keywords": ["FastAPI"],
}


class FakeRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def run(self, prompt, *, system, model, schema=None, session_id=None, resume=None):
        self.calls.append({"prompt": prompt, "system": system, "model": model, "schema": schema,
                           "session_id": session_id, "resume": resume})  # fmt: skip
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def vacancy(session, title="Senior Python Engineer", text=DESCRIPTION):
    from test_ai_analysis import add_vacancy

    return add_vacancy(session, title=title, text=text)


def worker(session, runner, tmp_path, cv=True):
    from sqlalchemy.orm import sessionmaker

    cv_path = tmp_path / "cv.md"
    if cv:
        cv_path.write_text("# Alex\nSenior Python developer, FastAPI, Django.")
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    return claude.Worker(factory, runner, profile_path=tmp_path / "profile.yaml", cv_path=cv_path)


def test_prompt_has_facts_profile_cv_and_vacancy():
    profile = Profile(known=["Python"], learnable=["RabbitMQ"], blockers=["TensorFlow"],
                      rules=["Only full remote."])  # fmt: skip
    text = analysis_prompt(cv="CV TEXT", profile=profile, title="Python Dev", company="Acme",
                           location=None, salary=None, description=DESCRIPTION)  # fmt: skip
    for part in [
        *FACTS,
        "Only full remote.",
        "RabbitMQ",
        "TensorFlow",
        "CV TEXT",
        "Acme",
        DESCRIPTION,
    ]:
        assert part in text
    assert "Location: unknown" in text


def test_enqueue_skips_pending_and_unknown(session):
    o = vacancy(session)
    assert claude.enqueue(session, [o.id, o.id, 999]) == 1
    assert claude.enqueue(session, [o.id]) == 0  # already queued
    status = claude.status(session)
    assert (status.queued, status.running, status.pending) == (1, 0, 1)


def test_worker_runs_one_and_checks_quotes(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    runner = FakeRunner([Reply("{}", dict(RESULT), "sess-1", 0.05)])
    assert worker(session, runner, tmp_path).run_once() is True
    session.expire_all()
    job = claude.latest(session, o.id)
    assert job.state == "done" and job.session_id == "sess-1" and job.cost_usd == 0.05
    assert job.result["location"]["quote_ok"] is True
    assert job.result["gaps"][0]["quote_ok"] is True
    assert job.result["red_flags"][0]["quote_ok"] is False  # not in the description
    call = runner.calls[0]
    assert call["schema"] == SCHEMA and call["model"] == "sonnet" and call["session_id"]
    assert "Senior Python developer, FastAPI" in call["prompt"]  # the CV
    assert "Russian" in call["system"]
    assert claude.status(session).recent == [o.id]
    # Nothing left to run.
    assert worker(session, FakeRunner([]), tmp_path).run_once() is False


def test_worker_failure_is_stored(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    worker(session, FakeRunner([ClaudeError("not logged in")]), tmp_path).run_once()
    session.expire_all()
    job = claude.latest(session, o.id)
    assert job.state == "failed" and job.error == "not logged in"
    # A failed one can be queued again.
    assert claude.enqueue(session, [o.id]) == 1


def test_vacancy_without_description_is_never_sent_to_claude(session, tmp_path):
    o = vacancy(session, text=None)
    assert claude.enqueue(session, [o.id]) == 0
    runner = FakeRunner([])
    assert worker(session, runner, tmp_path).run_once() is False
    assert runner.calls == []


def test_daily_cap_keeps_the_rest_queued(session, tmp_path):
    claude.save_claude_settings(session, claude.ClaudeSettings(daily_cap=1))
    a, b = vacancy(session, title="A"), vacancy(session, title="B")
    claude.enqueue(session, [a.id, b.id])
    w = worker(session, FakeRunner([Reply("{}", dict(RESULT), "s", None)]), tmp_path)
    assert w.run_once() is True
    assert w.run_once() is False
    status = claude.status(session)
    assert status.queued == 1 and status.capped
    # Tomorrow it runs.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s2", None)])
    assert w.run_once(now=tomorrow) is True


def test_restart_requeues_running_with_a_new_session(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    job = claude.latest(session, o.id)
    job.state, job.session_id = "running", "old"
    session.commit()
    w = worker(session, FakeRunner([]), tmp_path)
    w.idle_wait = 0.01
    w.start()
    w.stop()
    session.expire_all()
    job = session.get(ClaudeAnalysis, job.id)
    assert job.state in ("queued", "failed") and job.session_id != "old"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A stand-in `claude` that records its arguments and environment."""
    script = tmp_path / "claude"
    log = tmp_path / "calls.json"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
           "key": os.environ.get("ANTHROPIC_API_KEY")}}, open({str(log)!r}, "w"))
print(json.dumps({{"type": "result", "is_error": False, "result": "ok", "session_id": "s-9",
                  "structured_output": {{"verdict": "apply"}}, "total_cost_usd": 0.01}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    return script, log


def test_cli_runner_uses_print_mode_without_tools_or_api_key(fake_claude, tmp_path):
    script, log = fake_claude
    workdir = tmp_path / "work"
    reply = ClaudeCli(str(script), workdir).run(
        "the prompt", system="SYS", model="sonnet", schema={"type": "object"}, session_id="abc"
    )
    assert reply == Reply("ok", {"verdict": "apply"}, "s-9", 0.01)
    call = json.loads(log.read_text())
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--session-id") + 1] == "abc"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert call["prompt"] == "the prompt" and call["cwd"] == str(workdir)
    assert call["key"] is None  # never paid API billing


def test_cli_runner_reports_missing_command(tmp_path):
    with pytest.raises(ClaudeError, match="not found"):
        ClaudeCli(str(tmp_path / "nope"), tmp_path).run("x", system="s", model="sonnet")


def test_queue_button_and_row_badges(client, session):
    o = vacancy(session)
    text = client.get("/").text
    assert 'id="claude-analyze"' in text and f'class="claude-pick" value="{o.id}"' in text
    status = client.post("/claude/analyze", data={"ids": [o.id]}).text
    assert "1 queued" in status and 'hx-trigger="every 3s"' in status
    assert "Claude: queued" in client.get(f"/opportunities/{o.id}/row").text

    client.app.state.claude_worker.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    client.app.state.claude_worker.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: maybe · 70%" in row and "TensorFlow" in row and "(unverified)" in row
    assert "every 3s" not in client.get("/claude/status").text


def test_claude_settings(client, session):
    assert 'name="model"' in client.get("/settings").text
    client.post("/settings/claude", data={"model": "opus", "daily_cap": "5", "language": "English"})
    assert claude.load_claude_settings(session) == claude.ClaudeSettings("opus", 5, "English")
    assert client.post("/settings/claude", data={"model": "gpt"}).status_code == 422


def analyzed(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    worker(session, FakeRunner([Reply("{}", dict(RESULT), "sess-1", None)]), tmp_path).run_once()
    session.expire_all()
    return o


def test_chat_needs_a_finished_analysis(session):
    o = vacancy(session)
    assert claude.ask(session, o.id, "Hi?") is None


def test_chat_continues_the_analysis_session_and_goes_first(session, tmp_path):
    o = analyzed(session, tmp_path)
    other = vacancy(session, title="Other")
    claude.enqueue(session, [other.id])  # an analysis waits in the queue
    claude.ask(session, o.id, "Как ответить про TensorFlow?")
    assert claude.chat_pending(session, o.id)

    runner = FakeRunner([Reply("Честно скажите, что опыта нет.", None, "sess-1", None)])
    worker(session, runner, tmp_path).run_once()
    call = runner.calls[0]
    assert call["resume"] == "sess-1" and call["schema"] is None
    assert call["prompt"] == "Как ответить про TensorFlow?"
    assert "Answer in Russian" in call["system"]
    session.expire_all()
    roles = [(m.role, m.state, m.text) for m in claude.messages(session, o.id)]
    assert roles == [
        ("user", "done", "Как ответить про TensorFlow?"),
        ("assistant", "done", "Честно скажите, что опыта нет."),
    ]
    assert claude.latest(session, other.id).state == "queued"  # the analysis waited


def test_chat_failure_is_shown(session, tmp_path):
    o = analyzed(session, tmp_path)
    claude.ask(session, o.id, "Q?")
    worker(session, FakeRunner([ClaudeError("rate limited")]), tmp_path).run_once()
    session.expire_all()
    [m] = claude.messages(session, o.id)
    assert m.state == "failed" and m.error == "rate limited"


def test_vacancy_page_and_chat(client, session):
    o = vacancy(session)
    page = client.get(f"/opportunities/{o.id}").text
    assert "Senior Python Engineer" in page and "Professional TensorFlow" in page
    assert "Analyze with Claude" in page and "Ask Claude" not in page
    assert f'href="/opportunities/{o.id}"' in client.get("/").text  # page link in the queue

    section = client.post(f"/opportunities/{o.id}/claude").text
    assert "Claude: queued" in section and 'hx-trigger="every 3s"' in section
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "sess-1", None)])
    w.run_once()
    section = client.get(f"/opportunities/{o.id}/claude").text
    assert "Claude: maybe · 70%" in section and "Ask Claude" in section
    assert "every 3s" not in section

    asked = client.post(f"/opportunities/{o.id}/chat", data={"text": "Что спросить?"}).text
    assert "thinking…" in asked and "every 3s" in asked
    w.runner = FakeRunner([Reply("Спросите про контракт.", None, "sess-1", None)])
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    assert "Что спросить?" in page and "Спросите про контракт." in page
    assert client.post(f"/opportunities/{o.id}/chat", data={"text": " "}).status_code == 422


def test_unknown_vacancy_page(client):
    assert client.get("/opportunities/999").status_code == 404


def test_chat_markup_is_safe():
    from job_radar.web.app import chat_markup

    html = str(chat_markup("**Да** <script>x</script>\n> Speak fluent Dutch"))
    assert "<b>Да</b>" in html and "&lt;script&gt;" in html
    assert '<span class="quote">Speak fluent Dutch</span>' in html


def test_transient_errors_are_retried(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    runner = FakeRunner(
        [
            ClaudeError("error_max_structured_output_retries", transient=True),
            Reply("{}", dict(RESULT), "s", None),
        ]
    )
    w = worker(session, runner, tmp_path)
    w.retry_waits = (0, 0)
    w.run_once()
    session.expire_all()
    job = claude.latest(session, o.id)
    assert job.state == "done" and job.attempts == 2
    # Each attempt gets its own session id.
    assert runner.calls[0]["session_id"] != runner.calls[1]["session_id"]


def test_retries_stop_after_three_attempts(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    flaky = [ClaudeError("overloaded", transient=True)] * 3
    w = worker(session, FakeRunner(flaky), tmp_path)
    w.retry_waits = (0, 0)
    w.run_once()
    session.expire_all()
    job = claude.latest(session, o.id)
    assert job.state == "failed" and job.attempts == 3 and job.error == "overloaded"


def test_final_errors_are_not_retried(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    runner = FakeRunner([ClaudeError("Not logged in")])
    worker(session, runner, tmp_path).run_once()
    session.expire_all()
    assert claude.latest(session, o.id).attempts == 1 and len(runner.calls) == 1


def test_transient_classification():
    from job_radar.claude.runner import is_transient

    transient = [
        "error_max_structured_output_retries",
        "error_during_execution",
        "API Error: 529 Overloaded",
        "rate limit exceeded",
        "no answer in 600 s",
    ]
    for text in transient:
        assert is_transient(text), text
    for text in ("Not logged in · Please run /login", "`claude` not found"):
        assert not is_transient(text), text


def test_failed_row_has_a_retry_button(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    client.app.state.claude_worker.runner = FakeRunner([ClaudeError("Not logged in")])
    client.app.state.claude_worker.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude failed" in row and "1 attempt" in row
    assert f'hx-post="/opportunities/{o.id}/claude"' in row and ">retry<" in row


def test_row_links_to_the_chat(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert f'href="/opportunities/{o.id}#chat"' in row and "Chat with Claude →" in row
    assert 'id="chat"' in client.get(f"/opportunities/{o.id}").text


def test_card_button_and_select_toggle(client, session):
    o = vacancy(session)
    row = client.get("/").text
    assert f'hx-post="/opportunities/{o.id}/claude"' in row and "✨ Claude" in row
    assert 'class="btn btn-sm btn-outline-secondary select-toggle"' in row
    assert 'id="pick-bar"' in row and 'data-claude-action="analyze"' in row
    # The card button queues this one vacancy.
    client.post(f"/opportunities/{o.id}/claude")
    assert claude.latest(session, o.id).state == "queued"
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: queued" in row and "✨ Claude" not in row


def test_worker_can_be_switched_off(engine, database_url, tmp_path):
    from fastapi.testclient import TestClient

    from job_radar.config import Settings
    from job_radar.web.app import create_app

    settings = Settings(database_url=database_url, profile=tmp_path / "p.yaml", claude_worker=False)
    with TestClient(create_app(settings)) as client:
        assert client.app.state.claude_worker_on is False
        assert "Claude worker is off" in client.get("/claude/status").text


def test_claude_verdict_is_a_big_toggle(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert '<summary class="fold fold-claude">' in row and 'class="fold-hint"' in row


def test_chat_has_quick_prompts(client, session):
    from job_radar.claude.prompt import QUICK_PROMPTS

    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    labels = [label for label, _ in QUICK_PROMPTS]
    assert "Cover letter (EN)" in labels and "Сопроводительное (RU)" in labels
    for label in labels:
        assert f">{label}</button>" in page
    assert 'class="btn btn-sm btn-outline-secondary quick-prompt"' in page
    letter_en = dict(QUICK_PROMPTS)["Cover letter (EN)"]
    assert "in English" in letter_en and "do not invent" in letter_en.lower()
    assert "на русском" in dict(QUICK_PROMPTS)["Сопроводительное (RU)"]


def test_vacancies_without_description_are_not_queued(client, session):
    with_text = vacancy(session, title="With text")
    without = vacancy(session, title="No text", text=None)
    assert claude.enqueue(session, [with_text.id, without.id]) == 1
    assert claude.latest(session, without.id) is None
    status = client.post("/claude/analyze", data={"ids": [without.id]}).text
    assert "1 skipped: no description" in status
    row = client.get(f"/opportunities/{without.id}/row").text
    assert "✨ Claude" not in row and "no description" in row
    assert 'data-has-description="0"' in row


def test_finished_row_can_run_again(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "↻ again" in row and f'hx-post="/opportunities/{o.id}/claude"' in row
