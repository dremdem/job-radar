import json
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from job_radar.claude import analysis as claude
from job_radar.claude.analysis import ClaudeSettings
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

    def run(self, prompt, *, system, model, schema=None, session_id=None, resume=None, web=False):
        self.calls.append({"prompt": prompt, "system": system, "model": model, "schema": schema,
                           "session_id": session_id, "resume": resume, "web": web})  # fmt: skip
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
    assert call["web"] is False  # an analysis never goes online (#213)
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

    def settled():  # a pool thread may already have picked it up again
        session.expire_all()
        return session.get(ClaudeAnalysis, job.id).state in ("queued", "failed")

    assert wait_until(settled)
    w.stop()
    assert session.get(ClaudeAnalysis, job.id).session_id != "old"


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


def test_cli_runner_web_allows_search_but_not_linkedin_or_hh(fake_claude, tmp_path):
    script, log = fake_claude
    ClaudeCli(str(script), tmp_path).run("q", system="s", model="sonnet", resume="r", web=True)
    argv = json.loads(log.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--strict-mcp-config")]
    for domain in ("linkedin.com", "*.linkedin.com", "hh.ru", "*.hh.ru", "lnkd.in"):
        assert f"WebFetch(domain:{domain})" in denied


def test_chat_without_internet_when_switched_off(session, tmp_path):
    o = analyzed(session, tmp_path)
    claude.save_claude_settings(session, ClaudeSettings(chat_web=False))
    claude.ask(session, o.id, "Что за компания?")
    runner = FakeRunner([Reply("Не знаю.", None, "sess-1", None)])
    worker(session, runner, tmp_path).run_once()
    assert runner.calls[0]["web"] is False


def test_cli_runner_reports_missing_command(tmp_path):
    with pytest.raises(ClaudeError, match="not found"):
        ClaudeCli(str(tmp_path / "nope"), tmp_path).run("x", system="s", model="sonnet")


def test_queue_button_and_row_badges(client, session):
    o = vacancy(session)
    text = client.get("/").text
    assert 'id="analyze-top"' in text and 'data-has-description="1"' in text
    status = client.post("/claude/analyze", data={"ids": [o.id]}).text
    assert "1 queued" in status and 'hx-trigger="every 3s"' in status
    assert "Claude: queued" in client.get(f"/opportunities/{o.id}/row").text

    client.app.state.claude_worker.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    client.app.state.claude_worker.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: maybe · 70%" in row and 'data-claude="done"' in row
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "TensorFlow" in panel and "(unverified)" in panel
    assert "every 3s" not in client.get("/claude/status").text


def test_claude_settings(client, session):
    assert 'name="model"' in client.get("/settings").text
    client.post("/settings/claude", data={"model": "opus", "daily_cap": "5", "language": "English"})
    # An unchecked box is not sent: the chat goes offline (#213).
    off = claude.ClaudeSettings("opus", 5, "English", chat_web=False)
    assert claude.load_claude_settings(session) == off
    client.post("/settings/claude", data={"model": "opus", "chat_web": "on"})
    assert claude.load_claude_settings(session).chat_web is True
    assert 'name="chat_web" checked' in client.get("/settings").text
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
    assert call["web"] is True  # the chat may search the web (#213)
    assert "LinkedIn" in call["system"]  # and is told what it may not open
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
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "Claude failed" in panel and "1 attempt" in panel
    assert f'hx-post="/opportunities/{o.id}/claude"' in panel and ">Analyze again<" in panel


def test_row_links_to_the_chat(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    # The chat lives right in the panel and on the vacancy page.
    assert 'id="chat"' in client.get(f"/opportunities/{o.id}/panel").text
    assert 'id="chat"' in client.get(f"/opportunities/{o.id}").text


def test_card_button_and_select_toggle(client, session):
    o = vacancy(session)
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert f'hx-post="/opportunities/{o.id}/claude"' in panel and ">Analyze with Claude<" in panel
    # The panel button queues this one vacancy.
    client.post(f"/opportunities/{o.id}/claude")
    assert claude.latest(session, o.id).state == "queued"
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: queued" in row


def test_worker_can_be_switched_off(engine, database_url, tmp_path):
    from fastapi.testclient import TestClient

    from job_radar.config import Settings
    from job_radar.web.app import create_app

    settings = Settings(database_url=database_url, profile=tmp_path / "p.yaml", claude_worker=False)
    with TestClient(create_app(settings)) as client:
        assert client.app.state.claude_worker_on is False
        assert "Claude worker is off" in client.get("/claude/status").text


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


def test_find_original_posting_prompt():
    # #239: boards like We Work Remotely hide the apply link behind a paywall.
    from job_radar.claude.prompt import QUICK_PROMPTS

    text = dict(QUICK_PROMPTS)["Find original posting"].lower()
    for rule in ("careers page", "greenhouse", "lever", "ashby", "not found", "do not apply"):
        assert rule in text, rule
    assert "linkedin" in text and "hh.ru" in text  # never opened


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
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert ">Analyze again<" in panel and f'hx-post="/opportunities/{o.id}/claude"' in panel


def test_answers_have_a_copy_button(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    w.run_once()
    claude.ask(session, o.id, "Cover letter please")
    w.runner = FakeRunner([Reply("Dear team,\n<b>Alex</b>", None, "s", None)])
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    assert (
        page.count('class="btn btn-sm btn-outline-secondary mt-1 copy-answer"') == 1
    )  # answers only
    assert '<textarea class="msg-raw" hidden>Dear team,\n&lt;b&gt;Alex&lt;/b&gt;</textarea>' in page


def test_analyze_says_what_it_queued(client, session):
    vacancy(session)
    text = client.get("/").text
    assert 'id="analyze-top"' in text and ">Analyze…</button>" in text
    # A dialog with an editable minimum score, a preview and Start.
    assert '<dialog id="analyze-dialog"' in text and '<select id="analyze-min"' in text
    assert '<option value="1.0">' in text and '<option value="6.5" selected>' in text
    assert '<option value="10.0">' in text and '<option value="7.0">' in text
    assert "jr-analyze-min" in text and 'id="analyze-start"' in text
    assert 'document.querySelector("dialog[open]")' in text  # no I/X/A behind the dialog
    assert 'id="analyze-skipped"' in text and "already analysed or with a chat" in text
    assert "without a description" in text and '" queued"' in text and "setTimeout" in text


def test_chat_redraws_do_not_move_the_page(client, session):
    vacancy(session)
    text = client.get("/").text
    # Chrome's scroll anchoring jumped the panel away from the chat on every redraw (#188).
    assert "html { overflow-anchor: none; }" in text
    assert 'textarea[name=text]");' in text and "preventScroll: true" in text


class SlowRunner:
    """Counts how many runs overlap; each run waits until released."""

    def __init__(self, reply):
        self.reply, self.lock = reply, threading.Lock()
        self.active = self.peak = self.calls = 0
        self.release = threading.Event()

    def run(self, prompt, *, system, model, schema=None, session_id=None, resume=None, web=False):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        self.release.wait(5)
        with self.lock:
            self.active -= 1
        return self.reply


def wait_until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_analyses_run_in_parallel_up_to_the_setting(session, tmp_path):
    ids = [vacancy(session, title=f"Python Engineer {n}").id for n in range(5)]
    claude.enqueue(session, ids)
    claude.save_claude_settings(session, ClaudeSettings(parallel=3))
    runner = SlowRunner(Reply("{}", dict(RESULT), "s", None))
    w = worker(session, runner, tmp_path)
    w.idle_wait = 0.02
    w.start()
    try:
        assert wait_until(lambda: runner.active == 3)
        time.sleep(0.2)
        assert runner.peak == 3  # never more than the setting
        runner.release.set()
        assert wait_until(lambda: runner.calls == 5)

        def all_done():
            session.expire_all()
            return all(claude.latest(session, i).state == "done" for i in ids)

        assert wait_until(all_done)
    finally:
        w.stop()
    assert runner.calls == 5  # each vacancy exactly once: no double claim


def test_chat_is_answered_while_analyses_run(session, tmp_path):
    o = analyzed(session, tmp_path)
    other = vacancy(session, title="Python Engineer 2")
    claude.enqueue(session, [other.id])
    claude.save_claude_settings(session, ClaudeSettings(parallel=1))

    class Mixed(SlowRunner):
        def run(self, prompt, **kw):
            if kw.get("resume"):  # the chat
                return Reply("Ответ", None, "s", None)
            return super().run(prompt, **kw)

    runner = Mixed(Reply("{}", dict(RESULT), "s", None))
    w = worker(session, runner, tmp_path)
    w.idle_wait = 0.02
    w.start()
    try:
        assert wait_until(lambda: runner.active == 1)  # the only analysis slot is busy
        claude.ask(session, o.id, "Q?")
        w.wake()

        def answered():
            session.expire_all()
            return any(m.text == "Ответ" for m in claude.messages(session, o.id))

        assert wait_until(answered)
    finally:
        runner.release.set()
        w.stop()


def test_usage_limit_requeues_and_pauses(session, tmp_path):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    limit = ClaudeError("Claude AI usage limit reached|1760000000", transient=True)
    w = worker(session, FakeRunner([limit, Reply("{}", dict(RESULT), "s", None)]), tmp_path)
    assert w.run_once() is True
    session.expire_all()
    job = claude.latest(session, o.id)
    assert job.state == "queued" and job.attempts == 1  # back in the queue, not retried now
    assert w.paused_until is not None and "usage limit" in job.error
    assert w.run_once() is False  # paused: nothing starts
    assert w.run_once(now=w.paused_until + timedelta(seconds=1)) is True  # the pause is over
    session.expire_all()
    assert claude.latest(session, o.id).state == "done"


def test_parallel_setting_on_the_settings_page(client, session):
    assert 'name="parallel"' in client.get("/settings").text
    client.post("/settings/claude", data={"model": "sonnet", "daily_cap": 30,
                                          "language": "Russian", "parallel": 6})  # fmt: skip
    assert claude.load_claude_settings(session).parallel == 6
    assert ClaudeSettings().parallel == 4


def test_status_says_when_the_pause_ends(client, session):
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.paused_until = datetime.now(UTC) + timedelta(minutes=10)
    text = client.get("/claude/status").text
    assert "subscription limit: paused until" in text
    w.paused_until = None
    assert "paused until" not in client.get("/claude/status").text


def test_no_browser_notifications(client):
    assert "Notification" not in client.get("/").text  # #217
