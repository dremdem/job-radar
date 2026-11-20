"""Tests for job_radar.ai.claude: the prompt, the analysis queue and its workers, quote
checks and the chat, with a fake Claude runner (docs/claude.md).
"""

import datetime as dt
import json
import stat
import sys
import threading
import time

import claude_fakes
import pytest

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.prompt as claude_prompt
import job_radar.ai.claude.runner as claude_runner
import job_radar.models as models
import job_radar.profile.stack as profile_stack


def test_prompt_has_facts_profile_cv_and_vacancy():
    profile = profile_stack.Profile(
        known=["Python"],
        learnable=["RabbitMQ"],
        blockers=["TensorFlow"],
        rules=["Only full remote."],
    )
    text = claude_prompt.analysis_prompt(
        cv="CV TEXT",
        profile=profile,
        title="Python Dev",
        company="Acme",
        location=None,
        salary=None,
        description=claude_fakes.DESCRIPTION,
    )
    for part in [
        *claude_prompt.FACTS,
        "Only full remote.",
        "RabbitMQ",
        "TensorFlow",
        "CV TEXT",
        "Acme",
        claude_fakes.DESCRIPTION,
    ]:
        assert part in text
    assert "Location: unknown" in text
    # no home in the profile
    assert "will not relocate" not in text and "fully remote?" in text


def test_prompt_and_quick_prompts_take_home_from_the_profile():

    profile = profile_stack.Profile(home="Thailand")
    text = claude_prompt.analysis_prompt(
        cv=None,
        profile=profile,
        title="Python Dev",
        company="Acme",
        location=None,
        salary=None,
        description=claude_fakes.DESCRIPTION,
    )
    assert "I live in Thailand and will not relocate." in text
    assert "fully remote from Thailand?" in text
    prompts = dict(claude_prompt.quick_prompts(profile))
    assert "fully remote from Thailand" in prompts["Cover letter (EN)"]
    assert "(живу: Thailand)" in prompts["Сопроводительное (RU)"]
    assert "Thailand" not in " ".join(dict(claude_prompt.quick_prompts(None)).values())


def test_enqueue_skips_pending_and_unknown(session):
    o = claude_fakes.vacancy(session)
    assert claude_analysis.enqueue(session, [o.id, o.id, 999]) == 1
    # already queued
    assert claude_analysis.enqueue(session, [o.id]) == 0
    status = claude_analysis.status(session)
    assert (status.queued, status.running, status.pending) == (1, 0, 1)


def test_worker_runs_one_and_checks_quotes(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "sess-1", 0.05)]
    )
    assert claude_fakes.worker(session, runner, tmp_path).run_once() is True
    session.expire_all()
    job = claude_analysis.latest(session, o.id)
    assert job.state == "done" and job.session_id == "sess-1" and job.cost_usd == 0.05
    assert job.result["location"]["quote_ok"] is True
    assert job.result["gaps"][0]["quote_ok"] is True
    # not in the description
    assert job.result["red_flags"][0]["quote_ok"] is False
    call = runner.calls[0]
    assert (
        call["schema"] == claude_prompt.SCHEMA and call["model"] == "sonnet" and call["session_id"]
    )
    # an analysis never goes online (#213)
    assert call["web"] is False
    # the CV
    assert "Senior Python developer, FastAPI" in call["prompt"]
    assert "Russian" in call["system"]
    assert claude_analysis.status(session).recent == [o.id]
    # Nothing left to run.
    assert claude_fakes.worker(session, claude_fakes.FakeRunner([]), tmp_path).run_once() is False


def test_worker_failure_is_stored(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    claude_fakes.worker(
        session, claude_fakes.FakeRunner([claude_runner.ClaudeError("not logged in")]), tmp_path
    ).run_once()
    session.expire_all()
    job = claude_analysis.latest(session, o.id)
    assert job.state == "failed" and job.error == "not logged in"
    # A failed one can be queued again.
    assert claude_analysis.enqueue(session, [o.id]) == 1


def test_vacancy_without_description_is_never_sent_to_claude(session, tmp_path):
    o = claude_fakes.vacancy(session, text=None)
    assert claude_analysis.enqueue(session, [o.id]) == 0
    runner = claude_fakes.FakeRunner([])
    assert claude_fakes.worker(session, runner, tmp_path).run_once() is False
    assert runner.calls == []


def test_daily_cap_keeps_the_rest_queued(session, tmp_path):
    claude_analysis.save_claude_settings(session, claude_analysis.ClaudeSettings(daily_cap=1))
    a, b = claude_fakes.vacancy(session, title="A"), claude_fakes.vacancy(session, title="B")
    claude_analysis.enqueue(session, [a.id, b.id])
    w = claude_fakes.worker(
        session,
        claude_fakes.FakeRunner([claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]),
        tmp_path,
    )
    assert w.run_once() is True
    assert w.run_once() is False
    status = claude_analysis.status(session)
    assert status.queued == 1 and status.capped
    # Tomorrow it runs.
    tomorrow = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s2", None)]
    )
    assert w.run_once(now=tomorrow) is True


def test_restart_requeues_running_with_a_new_session(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    job = claude_analysis.latest(session, o.id)
    job.state, job.session_id = "running", "old"
    session.commit()
    w = claude_fakes.worker(session, claude_fakes.FakeRunner([]), tmp_path)
    w.idle_wait = 0.01
    w.start()

    # a pool thread may already have picked it up again
    def settled():
        session.expire_all()
        return session.get(models.ClaudeAnalysis, job.id).state in ("queued", "failed")

    assert wait_until(settled)
    w.stop()
    assert session.get(models.ClaudeAnalysis, job.id).session_id != "old"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A stand-in `claude` that records its arguments and environment."""
    script = tmp_path / "claude"
    log = tmp_path / "calls.json"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
           "key": os.environ.get("ANTHROPIC_API_KEY"),
           "auto_memory_off": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")}},
          open({str(log)!r}, "w"))
print(json.dumps({{"type": "result", "is_error": False, "result": "ok", "session_id": "s-9",
                  "structured_output": {{"verdict": "apply"}}, "total_cost_usd": 0.01}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    return script, log


def test_cli_runner_uses_print_mode_without_tools_or_api_key(fake_claude, tmp_path):
    script, log = fake_claude
    workdir = tmp_path / "work"
    reply = claude_runner.ClaudeCli(str(script), workdir).run(
        "the prompt", system="SYS", model="sonnet", schema={"type": "object"}, session_id="abc"
    )
    assert reply == claude_runner.Reply("ok", {"verdict": "apply"}, "s-9", 0.01)
    call = json.loads(log.read_text())
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--session-id") + 1] == "abc"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert call["prompt"] == "the prompt" and call["cwd"] == str(workdir)
    # never paid API billing
    assert call["key"] is None
    # nor Claude Code's own auto-memory of the repository the workdir is in (#48)
    assert call["auto_memory_off"] == "1"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"


def test_cli_runner_resume_sends_the_instructions_in_the_message(fake_claude, tmp_path):
    """Claude Code ignores --system-prompt on --resume (#48), so a resumed run (the vacancy
    chat) carries the instructions at the start of its message."""
    script, log = fake_claude
    claude_runner.ClaudeCli(str(script), tmp_path).run(
        "Запомни это.", system="CHAT RULES", model="sonnet", resume="r"
    )
    call = json.loads(log.read_text())
    assert "--system-prompt" not in call["argv"]
    assert call["argv"][call["argv"].index("--resume") + 1] == "r"
    instructions, question = call["prompt"].split("</job-radar-instructions>")
    assert instructions.startswith("<job-radar-instructions>") and "CHAT RULES" in instructions
    assert question.strip() == "Запомни это."


def test_cli_runner_web_allows_search_but_not_linkedin_or_hh(fake_claude, tmp_path):
    script, log = fake_claude
    claude_runner.ClaudeCli(str(script), tmp_path).run(
        "q", system="s", model="sonnet", resume="r", web=True
    )
    argv = json.loads(log.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--strict-mcp-config")]
    for domain in ("linkedin.com", "*.linkedin.com", "hh.ru", "*.hh.ru", "lnkd.in"):
        assert f"WebFetch(domain:{domain})" in denied


def test_chat_without_internet_when_switched_off(session, tmp_path):
    o = claude_fakes.analyzed(session, tmp_path)
    claude_analysis.save_claude_settings(session, claude_analysis.ClaudeSettings(chat_web=False))
    claude_analysis.ask(session, o.id, "Что за компания?")
    runner = claude_fakes.FakeRunner([claude_runner.Reply("Не знаю.", None, "sess-1", None)])
    claude_fakes.worker(session, runner, tmp_path).run_once()
    assert runner.calls[0]["web"] is False


def test_cli_runner_reports_missing_command(tmp_path):
    with pytest.raises(claude_runner.ClaudeError, match="not found"):
        claude_runner.ClaudeCli(str(tmp_path / "nope"), tmp_path).run(
            "x", system="s", model="sonnet"
        )


def test_queue_button_and_row_badges(client, session):
    o = claude_fakes.vacancy(session)
    text = client.get("/").text
    assert 'id="analyze-top"' in text and 'data-has-description="1"' in text
    status = client.post("/claude/analyze", data={"ids": [o.id]}).text
    assert "1 queued" in status and 'hx-trigger="every 3s"' in status
    assert "Claude: queued" in client.get(f"/opportunities/{o.id}/row").text

    client.app.state.claude_worker.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
    client.app.state.claude_worker.run_once()
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: maybe · 70%" in row and 'data-claude="done"' in row
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "TensorFlow" in panel and "(unverified)" in panel
    assert "every 3s" not in client.get("/claude/status").text


def test_claude_settings(client, session):
    assert 'name="model"' in client.get("/settings").text
    client.post("/settings/claude", data={"model": "opus", "daily_cap": "5", "language": "English"})
    # An unchecked box is not sent: the chat goes offline (#213), and new descriptions are no
    # longer queued by themselves (#264).
    off = claude_analysis.ClaudeSettings("opus", 5, "English", chat_web=False, auto_analyze=False)
    assert claude_analysis.load_claude_settings(session) == off
    client.post("/settings/claude", data={"model": "opus", "chat_web": "on"})
    assert claude_analysis.load_claude_settings(session).chat_web is True
    assert 'name="chat_web" checked' in client.get("/settings").text
    assert client.post("/settings/claude", data={"model": "gpt"}).status_code == 422


def test_chat_needs_a_finished_analysis(session):
    o = claude_fakes.vacancy(session)
    assert claude_analysis.ask(session, o.id, "Hi?") is None


def test_chat_continues_the_analysis_session_and_goes_first(session, tmp_path):
    o = claude_fakes.analyzed(session, tmp_path)
    other = claude_fakes.vacancy(session, title="Other")
    # an analysis waits in the queue
    claude_analysis.enqueue(session, [other.id])
    claude_analysis.ask(session, o.id, "Как ответить про TensorFlow?")
    assert claude_analysis.chat_pending(session, o.id)

    runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("Честно скажите, что опыта нет.", None, "sess-1", None)]
    )
    claude_fakes.worker(session, runner, tmp_path).run_once()
    call = runner.calls[0]
    assert call["resume"] == "sess-1" and call["schema"] is None
    # the chat may search the web (#213)
    assert call["web"] is True
    # and is told what it may not open
    assert "LinkedIn" in call["system"]
    assert call["prompt"] == "Как ответить про TensorFlow?"
    assert "Answer in Russian" in call["system"]
    session.expire_all()
    roles = [(m.role, m.state, m.text) for m in claude_analysis.messages(session, o.id)]
    assert roles == [
        ("user", "done", "Как ответить про TensorFlow?"),
        ("assistant", "done", "Честно скажите, что опыта нет."),
    ]
    # the analysis waited
    assert claude_analysis.latest(session, other.id).state == "queued"


def test_chat_failure_is_shown(session, tmp_path):
    o = claude_fakes.analyzed(session, tmp_path)
    claude_analysis.ask(session, o.id, "Q?")
    claude_fakes.worker(
        session, claude_fakes.FakeRunner([claude_runner.ClaudeError("rate limited")]), tmp_path
    ).run_once()
    session.expire_all()
    [m] = claude_analysis.messages(session, o.id)
    assert m.state == "failed" and m.error == "rate limited"


def test_vacancy_page_and_chat(client, session):
    o = claude_fakes.vacancy(session)
    page = client.get(f"/opportunities/{o.id}").text
    assert "Senior Python Engineer" in page and "Professional TensorFlow" in page
    assert "Analyze with Claude" in page and "Ask Claude" not in page
    # page link in the queue
    assert f'href="/opportunities/{o.id}"' in client.get("/").text

    section = client.post(f"/opportunities/{o.id}/claude").text
    assert "Claude: queued" in section and 'hx-trigger="every 3s"' in section
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "sess-1", None)]
    )
    w.run_once()
    section = client.get(f"/opportunities/{o.id}/claude").text
    assert "Claude: maybe · 70%" in section and "Ask Claude" in section
    assert "every 3s" not in section

    asked = client.post(f"/opportunities/{o.id}/chat", data={"text": "Что спросить?"}).text
    assert "thinking…" in asked and "every 3s" in asked
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("Спросите про контракт.", None, "sess-1", None)]
    )
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    assert "Что спросить?" in page and "Спросите про контракт." in page
    assert client.post(f"/opportunities/{o.id}/chat", data={"text": " "}).status_code == 422


def test_unknown_vacancy_page(client):
    assert client.get("/opportunities/999").status_code == 404


def test_chat_markup_is_safe():
    import job_radar.web.templating as web_templating

    html = str(web_templating.chat_markup("**Да** <script>x</script>\n> Speak fluent Dutch"))
    assert "<b>Да</b>" in html and "&lt;script&gt;" in html
    assert '<span class="quote">Speak fluent Dutch</span>' in html


def test_chat_links_are_clickable():
    # #241: links in answers had to be copied by hand.
    import job_radar.web.templating as web_templating

    a = '<a href="{0}" target="_blank" rel="noopener noreferrer">{1}</a>'
    url = "https://job-boards.greenhouse.io/planetscale/jobs/4251150009"
    html = str(web_templating.chat_markup(f"- **Direct link:** {url}."))
    assert a.format(url, url) + "." in html
    html = str(web_templating.chat_markup(f"- [PlanetScale Greenhouse posting]({url})"))
    assert a.format(url, "PlanetScale Greenhouse posting") in html
    html = str(web_templating.chat_markup("(see https://example.com/a?x=1&y=2)"))
    assert (
        a.format("https://example.com/a?x=1&amp;y=2", "https://example.com/a?x=1&amp;y=2") + ")"
        in html
    )
    html = str(web_templating.chat_markup('[x](javascript:alert(1)) https://e.com/"onmouseover=x'))
    assert "javascript:" not in html.replace("[x](javascript:alert(1))", "")
    assert 'href="javascript' not in html and '"onmouseover' not in html


def test_description_links_are_clickable_and_python_still_marked():
    # #243: the way to apply is often a link in the description.
    import job_radar.scoring.description as scoring_description

    url = "https://jobs.lever.co/python-co/1"
    html = str(scoring_description.highlight_python(f"Python role. Apply: {url}. <b>"))
    assert "<mark>Python</mark> role" in html
    assert f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>.' in html
    assert "&lt;b&gt;" in html


def test_transient_errors_are_retried(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    runner = claude_fakes.FakeRunner(
        [
            claude_runner.ClaudeError("error_max_structured_output_retries", transient=True),
            claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None),
        ]
    )
    w = claude_fakes.worker(session, runner, tmp_path)
    w.retry_waits = (0, 0)
    w.run_once()
    session.expire_all()
    job = claude_analysis.latest(session, o.id)
    assert job.state == "done" and job.attempts == 2
    # Each attempt gets its own session id.
    assert runner.calls[0]["session_id"] != runner.calls[1]["session_id"]


def test_retries_stop_after_three_attempts(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    flaky = [claude_runner.ClaudeError("overloaded", transient=True)] * 3
    w = claude_fakes.worker(session, claude_fakes.FakeRunner(flaky), tmp_path)
    w.retry_waits = (0, 0)
    w.run_once()
    session.expire_all()
    job = claude_analysis.latest(session, o.id)
    assert job.state == "failed" and job.attempts == 3 and job.error == "overloaded"


def test_final_errors_are_not_retried(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    runner = claude_fakes.FakeRunner([claude_runner.ClaudeError("Not logged in")])
    claude_fakes.worker(session, runner, tmp_path).run_once()
    session.expire_all()
    assert claude_analysis.latest(session, o.id).attempts == 1 and len(runner.calls) == 1


def test_transient_classification():

    transient = [
        "error_max_structured_output_retries",
        "error_during_execution",
        "API Error: 529 Overloaded",
        "rate limit exceeded",
        "no answer in 600 s",
    ]
    for text in transient:
        assert claude_runner.is_transient(text), text
    for text in ("Not logged in · Please run /login", "`claude` not found"):
        assert not claude_runner.is_transient(text), text


def test_failed_row_has_a_retry_button(client, session):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    client.app.state.claude_worker.runner = claude_fakes.FakeRunner(
        [claude_runner.ClaudeError("Not logged in")]
    )
    client.app.state.claude_worker.run_once()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "Claude failed" in panel and "1 attempt" in panel
    assert f'hx-post="/opportunities/{o.id}/claude"' in panel and ">Analyze again<" in panel


def test_row_links_to_the_chat(client, session):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
    w.run_once()
    # The chat lives right in the panel and on the vacancy page.
    assert 'id="chat"' in client.get(f"/opportunities/{o.id}/panel").text
    assert 'id="chat"' in client.get(f"/opportunities/{o.id}").text


def test_card_button_and_select_toggle(client, session):
    o = claude_fakes.vacancy(session)
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert f'hx-post="/opportunities/{o.id}/claude"' in panel and ">Analyze with Claude<" in panel
    # The panel button queues this one vacancy.
    client.post(f"/opportunities/{o.id}/claude")
    assert claude_analysis.latest(session, o.id).state == "queued"
    row = client.get(f"/opportunities/{o.id}/row").text
    assert "Claude: queued" in row


def test_worker_can_be_switched_off(engine, database_url, tmp_path):
    import fastapi.testclient as fastapi_testclient

    import job_radar.config as config
    import job_radar.web.app as web_app

    settings = config.Settings(
        database_url=database_url, profile=tmp_path / "p.yaml", claude_worker=False
    )
    with fastapi_testclient.TestClient(web_app.create_app(settings)) as client:
        assert client.app.state.claude_worker_on is False
        assert "Claude worker is off" in client.get("/claude/status").text


def test_chat_has_quick_prompts(client, session):

    QUICK_PROMPTS = claude_prompt.quick_prompts(None)
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
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

    text = dict(claude_prompt.quick_prompts(None))["Find original posting"].lower()
    for rule in ("careers page", "greenhouse", "lever", "ashby", "not found", "do not apply"):
        assert rule in text, rule
    # never opened
    assert "linkedin" in text and "hh.ru" in text


def test_vacancies_without_description_are_not_queued(client, session):
    with_text = claude_fakes.vacancy(session, title="With text")
    without = claude_fakes.vacancy(session, title="No text", text=None)
    assert claude_analysis.enqueue(session, [with_text.id, without.id]) == 1
    assert claude_analysis.latest(session, without.id) is None
    status = client.post("/claude/analyze", data={"ids": [without.id]}).text
    assert "1 skipped: no description" in status
    row = client.get(f"/opportunities/{without.id}/row").text
    assert "✨ Claude" not in row and "no description" in row
    assert 'data-has-description="0"' in row


def test_finished_row_can_run_again(client, session):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
    w.run_once()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert ">Analyze again<" in panel and f'hx-post="/opportunities/{o.id}/claude"' in panel


def test_answers_have_a_copy_button(client, session):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
    w.run_once()
    claude_analysis.ask(session, o.id, "Cover letter please")
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("Dear team,\n<b>Alex</b>", None, "s", None)]
    )
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    assert (
        page.count('class="btn btn-sm btn-outline-secondary mt-1 copy-answer"') == 1
        # answers only
    )
    assert '<textarea class="msg-raw" hidden>Dear team,\n&lt;b&gt;Alex&lt;/b&gt;</textarea>' in page


def test_copy_takes_only_the_fenced_letter(client, session):
    # #54: Copy put Claude's note after the letter on the clipboard too.
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
    )
    w.run_once()
    claude_analysis.ask(session, o.id, "Cover letter please")
    answer = (
        "Here it is:\n\n```\nDear team,\n\nBest regards\n```\n\nBefore you send it, check the hub."
    )
    w.runner = claude_fakes.FakeRunner([claude_runner.Reply(answer, None, "s", None)])
    w.run_once()
    page = client.get(f"/opportunities/{o.id}").text
    assert '<textarea class="msg-raw" hidden>Dear team,\n\nBest regards</textarea>' in page
    # the fence lines are not shown, the letter stands out as a block
    assert "```" not in page.split('<div class="chat">')[1]
    assert '<span class="paste">Dear team,\n\nBest regards</span>' in page


def test_paste_text():
    import job_radar.web.templating as web_templating

    # no fence: the whole answer, as before
    assert web_templating.paste_text("Dear team,\nAlex") == "Dear team,\nAlex"
    # a fence with a language tag; text around it is left out
    assert web_templating.paste_text("Note\n```text\nHi\n```\nAfter") == "Hi"
    # subject and body in two blocks
    two = "```\nSubject: Python role\n```\nand\n```\nHello\n```"
    assert web_templating.paste_text(two) == "Subject: Python role\n\nHello"


def test_chat_prompt_asks_to_fence_text_to_send():
    for web in (False, True):
        text = claude_prompt.chat_system_prompt("English", web=web)
        assert "```" in text and "outside" in text
        # #61: answers to application form questions are pasted too
        assert "application form" in text


def test_analyze_says_what_it_queued(client, session):
    claude_fakes.vacancy(session)
    text = client.get("/").text
    assert 'id="analyze-top"' in text and ">Analyze…</button>" in text
    # A dialog with an editable minimum score, a preview and Start.
    assert '<dialog id="analyze-dialog"' in text and '<select id="analyze-min"' in text
    assert '<option value="1.0">' in text and '<option value="6.5" selected>' in text
    assert '<option value="10.0">' in text and '<option value="7.0">' in text
    assert "jr-analyze-min" in text and 'id="analyze-start"' in text
    # no I/X/A behind the dialog
    assert 'document.querySelector("dialog[open]")' in text
    assert 'id="analyze-skipped"' in text and "already analysed or with a chat" in text
    assert "without a description" in text and '" queued"' in text and "setTimeout" in text


def test_chat_redraws_do_not_move_the_page(client, session):
    claude_fakes.vacancy(session)
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
    ids = [claude_fakes.vacancy(session, title=f"Python Engineer {n}").id for n in range(5)]
    claude_analysis.enqueue(session, ids)
    claude_analysis.save_claude_settings(session, claude_analysis.ClaudeSettings(parallel=3))
    runner = SlowRunner(claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None))
    w = claude_fakes.worker(session, runner, tmp_path)
    w.idle_wait = 0.02
    w.start()
    try:
        assert wait_until(lambda: runner.active == 3)
        time.sleep(0.2)
        # never more than the setting
        assert runner.peak == 3
        runner.release.set()
        assert wait_until(lambda: runner.calls == 5)

        def all_done():
            session.expire_all()
            return all(claude_analysis.latest(session, i).state == "done" for i in ids)

        assert wait_until(all_done)
    finally:
        w.stop()
    # each vacancy exactly once: no double claim
    assert runner.calls == 5


def test_chat_is_answered_while_analyses_run(session, tmp_path):
    o = claude_fakes.analyzed(session, tmp_path)
    other = claude_fakes.vacancy(session, title="Python Engineer 2")
    claude_analysis.enqueue(session, [other.id])
    claude_analysis.save_claude_settings(session, claude_analysis.ClaudeSettings(parallel=1))

    class Mixed(SlowRunner):
        def run(self, prompt, **kw):
            # the chat
            if kw.get("resume"):
                return claude_runner.Reply("Ответ", None, "s", None)
            return super().run(prompt, **kw)

    runner = Mixed(claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None))
    w = claude_fakes.worker(session, runner, tmp_path)
    w.idle_wait = 0.02
    w.start()
    try:
        # the only analysis slot is busy
        assert wait_until(lambda: runner.active == 1)
        claude_analysis.ask(session, o.id, "Q?")
        w.wake()

        def answered():
            session.expire_all()
            return any(m.text == "Ответ" for m in claude_analysis.messages(session, o.id))

        assert wait_until(answered)
    finally:
        runner.release.set()
        w.stop()


def test_usage_limit_requeues_and_pauses(session, tmp_path):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    limit = claude_runner.ClaudeError("Claude AI usage limit reached|1760000000", transient=True)
    w = claude_fakes.worker(
        session,
        claude_fakes.FakeRunner(
            [limit, claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]
        ),
        tmp_path,
    )
    assert w.run_once() is True
    session.expire_all()
    job = claude_analysis.latest(session, o.id)
    # back in the queue, not retried now
    assert job.state == "queued" and job.attempts == 1
    assert w.paused_until is not None and "usage limit" in job.error
    # paused: nothing starts
    assert w.run_once() is False
    # the pause is over
    assert w.run_once(now=w.paused_until + dt.timedelta(seconds=1)) is True
    session.expire_all()
    assert claude_analysis.latest(session, o.id).state == "done"


def test_parallel_setting_on_the_settings_page(client, session):
    assert 'name="parallel"' in client.get("/settings").text
    client.post("/settings/claude", data={"model": "sonnet", "daily_cap": 30,
                                          "language": "Russian", "parallel": 6})  # fmt: skip
    assert claude_analysis.load_claude_settings(session).parallel == 6
    assert claude_analysis.ClaudeSettings().parallel == 4


def test_status_says_when_the_pause_ends(client, session):
    o = claude_fakes.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    w = client.app.state.claude_worker
    w.paused_until = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
    text = client.get("/claude/status").text
    assert "subscription limit: paused until" in text
    w.paused_until = None
    assert "paused until" not in client.get("/claude/status").text


def test_no_browser_notifications(client):
    # #217
    assert "Notification" not in client.get("/").text


def test_finished_skip_auto_ignores_when_the_reason_is_chosen(session, tmp_path):
    import job_radar.review.auto_ignore as review_auto_ignore

    review_auto_ignore.save_rules(
        session, review_auto_ignore.Rules(countries=(), claude_kinds=("location",))
    )
    eu, stack = (
        claude_fakes.vacancy(session, title="EU job"),
        claude_fakes.vacancy(session, title="Stack job"),
    )
    claude_analysis.enqueue(session, [eu.id, stack.id])
    skip = dict(claude_fakes.RESULT, verdict="skip")
    w = claude_fakes.worker(
        session,
        claude_fakes.FakeRunner(
            [
                claude_runner.Reply(
                    "{}",
                    skip | {"ignore_reason": {"kind": "location", "detail": "EU only"}},
                    "s1",
                    None,
                ),
                claude_runner.Reply(
                    "{}",
                    skip | {"ignore_reason": {"kind": "stack", "detail": "TensorFlow"}},
                    "s2",
                    None,
                ),
            ]
        ),
        tmp_path,
    )
    assert w.run_once() and w.run_once()
    session.expire_all()
    assert (eu.state, eu.auto_ignore_reason) == (
        models.OpportunityState.AUTO_IGNORE,
        "Claude: location: EU only",
    )
    assert (stack.state, stack.auto_ignore_reason) == (models.OpportunityState.NEW, None)


# --- new descriptions are analyzed by themselves (#264) ------------------------


def test_new_descriptions_are_queued_by_the_worker(session, tmp_path):

    fresh = claude_fakes.vacancy(session, title="Fresh")
    # nothing to analyze yet
    claude_fakes.vacancy(session, title="No text", text=None)
    claude_fakes.vacancy(session, title="Ignored").state = models.OpportunityState.IGNORE
    done = claude_fakes.vacancy(session, title="Done before")
    session.add(models.ClaudeAnalysis(opportunity_id=done.id, model="sonnet", prompt_version="3",
                                  state="failed"))  # fmt: skip
    session.commit()
    runner = claude_fakes.FakeRunner(
        [claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s1", None)]
    )
    w = claude_fakes.worker(session, runner, tmp_path)
    # queued and run, no Analyze button pressed
    assert w.run_once() is True
    assert len(runner.calls) == 1 and "Fresh" in runner.calls[0]["prompt"]
    # never twice, and not the others
    assert w.run_once() is False
    session.expire_all()
    assert claude_analysis.latest(session, fresh.id).state == "done"


def test_a_description_arriving_later_is_queued_then(session, tmp_path):
    import job_radar.dedup as dedup

    o = claude_fakes.vacancy(session, text=None)
    w = claude_fakes.worker(
        session,
        claude_fakes.FakeRunner([claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s1", None)]),
        tmp_path,
    )
    assert w.run_once() is False
    # Descriptions, Chrome fill, …
    dedup.add_snapshot(session, o.source_records[0], claude_fakes.DESCRIPTION)
    session.commit()
    assert w.run_once() is True


def test_auto_analysis_can_be_switched_off(session, tmp_path):
    claude_analysis.save_claude_settings(
        session, claude_analysis.ClaudeSettings(auto_analyze=False)
    )
    claude_fakes.vacancy(session)
    runner = claude_fakes.FakeRunner([])
    assert claude_fakes.worker(session, runner, tmp_path).run_once() is False
    assert runner.calls == [] and claude_analysis.status(session).queued == 0


def test_auto_analysis_is_on_by_default_and_on_the_settings_page(client, session):
    assert claude_analysis.load_claude_settings(session).auto_analyze is True
    assert 'name="auto_analyze" checked' in client.get("/settings").text
    # the box unticked
    client.post("/settings/claude", data={"model": "sonnet"})
    assert claude_analysis.load_claude_settings(session).auto_analyze is False
    client.post("/settings/claude", data={"model": "sonnet", "auto_analyze": "on"})
    assert claude_analysis.load_claude_settings(session).auto_analyze is True


def test_auto_queue_respects_the_daily_cap(session, tmp_path):
    claude_analysis.save_claude_settings(session, claude_analysis.ClaudeSettings(daily_cap=1))
    claude_fakes.vacancy(session, title="A"), claude_fakes.vacancy(session, title="B")
    w = claude_fakes.worker(
        session,
        claude_fakes.FakeRunner([claude_runner.Reply("{}", dict(claude_fakes.RESULT), "s", None)]),
        tmp_path,
    )
    assert w.run_once() is True and w.run_once() is False
    status = claude_analysis.status(session)
    # waits for tomorrow, like the button's
    assert status.queued == 1 and status.capped
