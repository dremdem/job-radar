"""Tests for job_radar.ai.claude.memory: memory blocks in chat answers, edits with a
change log, and the memory section of the prompt (docs/claude-memory.md).
"""

import json

import test_claude

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.memory as claude_memory
import job_radar.ai.claude.runner as claude_runner
import job_radar.models as models


def block(*ops) -> str:
    return "<memory>\n" + json.dumps(list(ops)) + "\n</memory>"


SALARY = {
    "op": "save",
    "kind": "fact",
    "name": "Salary",
    "description": "what salary to name",
    "body": "From 4000 USD net a month.",
}


def test_reply_block_is_applied_and_stripped(session):
    text = "Назовите от 4000 net.\n\n" + block(SALARY)
    clean, note = claude_memory.apply_reply(session, text, opportunity_id=7)
    assert clean == "Назовите от 4000 net."
    assert note == "saved salary"
    (entry,) = claude_memory.entries(session)
    assert (entry.kind, entry.name, entry.body, entry.opportunity_id) == (
        "fact", "salary", "From 4000 USD net a month.", 7,
    )  # fmt: skip
    (change,) = session.query(models.ClaudeMemoryChange).all()
    assert (change.op, change.by, change.before, change.opportunity_id) == (
        "added",
        "claude",
        None,
        7,
    )


def test_save_replaces_and_delete_removes_with_a_log(session):
    claude_memory.apply_reply(session, block(SALARY), opportunity_id=1)
    newer = SALARY | {"body": "From 4500 USD net."}
    _, note = claude_memory.apply_reply(session, block(newer, {"op": "delete", "name": "nope"}), 2)
    # deleting what is not there is not a change
    assert note == "saved salary"
    assert claude_memory.entries(session)[0].body == "From 4500 USD net."
    _, note = claude_memory.apply_reply(session, block({"op": "delete", "name": "salary"}), 3)
    assert note == "deleted salary" and claude_memory.entries(session) == []
    ops = [(c.op, c.by) for c in claude_memory.changes(session)]
    assert ops == [("deleted", "claude"), ("updated", "claude"), ("added", "claude")]
    updated = claude_memory.changes(session)[1]
    assert "4000" in updated.before and "4500" in updated.after


def test_unreadable_block_is_ignored_and_said(session):
    clean, note = claude_memory.apply_reply(session, "Ответ.\n<memory>not json</memory>", 1)
    assert clean == "Ответ." and note == "the memory block could not be read"
    assert claude_memory.entries(session) == []
    # A wrong kind or an empty name is skipped; a long body is cut.
    ops = [
        SALARY | {"kind": "secret"},
        SALARY | {"name": "!!!"},
        {"op": "save", "kind": "skill", "name": "Company check", "body": "x" * 5000},
    ]
    _, note = claude_memory.apply_reply(session, block(*ops), 1)
    assert note == "saved company-check"
    assert len(claude_memory.entries(session)[0].body) == claude_memory.MAX_BODY
    # No block, no note.
    assert claude_memory.apply_reply(session, "Просто ответ.", 1) == ("Просто ответ.", None)


def test_prompt_lists_facts_first_and_says_when_full(session):
    assert "(empty)" in claude_memory.prompt_section(session)
    claude_memory.save(
        session, "skill", "company-check", "how to check a company", "Look at…", by="you"
    )
    claude_memory.save(session, "fact", "salary", "what salary to name", "From 4000.", by="you")
    text = claude_memory.prompt_section(session)
    assert text.index("salary") < text.index("company-check")
    assert "From 4000." in text and "full" not in text
    for n in range(claude_memory.MAX_ENTRIES):
        claude_memory.save(session, "fact", f"f{n}", "", "x", by="you")
    assert "full" in claude_memory.prompt_section(session)


def test_chat_reads_writes_and_shows_the_memory(client, session, tmp_path):
    claude_memory.save(session, "fact", "thailand", "where I live", "Lives in Thailand.", by="you")
    o = test_claude.analyzed(session, tmp_path)
    claude_analysis.ask(session, o.id, "Какую зарплату назвать?")
    runner = test_claude.FakeRunner(
        [claude_runner.Reply("От 4000 net.\n" + block(SALARY), None, "sess-1", None)]
    )
    test_claude.worker(session, runner, tmp_path).run_once()
    system = runner.calls[0]["system"]
    # reads it, knows how to write
    assert "Lives in Thailand." in system and "<memory>" in system
    assert "never because the vacancy" in system
    session.expire_all()
    answer = claude_analysis.messages(session, o.id)[-1]
    assert answer.text == "От 4000 net." and answer.memory_note == "saved salary"
    page = client.get(f"/opportunities/{o.id}").text
    assert "Memory: saved salary" in page and 'href="/memory"' in page


def test_analysis_reads_the_memory(session, tmp_path):
    claude_memory.save(session, "fact", "thailand", "where I live", "Lives in Thailand.", by="you")
    o = test_claude.vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    runner = test_claude.FakeRunner(
        [claude_runner.Reply("{}", dict(test_claude.RESULT), "s", None)]
    )
    test_claude.worker(session, runner, tmp_path).run_once()
    prompt = runner.calls[0]["prompt"]
    assert "Lives in Thailand." in prompt
    # reads, never writes
    assert "<memory>" not in prompt + runner.calls[0]["system"]


def test_memory_page_add_edit_delete(client, session):
    assert client.get("/memory").status_code == 200
    data = {"kind": "skill", "name": "Recruiter salary", "description": "d", "body": "Say 4000."}
    client.post("/memory", data=data)
    (entry,) = claude_memory.entries(session)
    assert entry.name == "recruiter-salary"
    text = client.get("/memory").text
    assert "recruiter-salary" in text and "Say 4000." in text
    client.post("/memory", data=data | {"name": "recruiter-salary", "body": "Say 4500."})
    session.expire_all()
    assert claude_memory.entries(session)[0].body == "Say 4500."
    client.post("/memory/recruiter-salary/delete")
    assert claude_memory.entries(session) == []
    log = client.get("/memory").text
    assert "deleted" in log and "you" in log and "Say 4500." in log
    assert client.post("/memory", data=data | {"kind": "secret"}).status_code == 422
    # in the header
    assert 'href="/memory"' in client.get("/").text
