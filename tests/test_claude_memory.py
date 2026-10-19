import json

from test_claude import RESULT, FakeRunner, analyzed, vacancy, worker

from job_radar.claude import analysis as claude
from job_radar.claude import memory
from job_radar.claude.runner import Reply
from job_radar.models import ClaudeMemoryChange


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
    clean, note = memory.apply_reply(session, text, opportunity_id=7)
    assert clean == "Назовите от 4000 net."
    assert note == "saved salary"
    (entry,) = memory.entries(session)
    assert (entry.kind, entry.name, entry.body, entry.opportunity_id) == (
        "fact", "salary", "From 4000 USD net a month.", 7,
    )  # fmt: skip
    (change,) = session.query(ClaudeMemoryChange).all()
    assert (change.op, change.by, change.before, change.opportunity_id) == (
        "added",
        "claude",
        None,
        7,
    )


def test_save_replaces_and_delete_removes_with_a_log(session):
    memory.apply_reply(session, block(SALARY), opportunity_id=1)
    newer = SALARY | {"body": "From 4500 USD net."}
    _, note = memory.apply_reply(session, block(newer, {"op": "delete", "name": "nope"}), 2)
    assert note == "saved salary"  # deleting what is not there is not a change
    assert memory.entries(session)[0].body == "From 4500 USD net."
    _, note = memory.apply_reply(session, block({"op": "delete", "name": "salary"}), 3)
    assert note == "deleted salary" and memory.entries(session) == []
    ops = [(c.op, c.by) for c in memory.changes(session)]
    assert ops == [("deleted", "claude"), ("updated", "claude"), ("added", "claude")]
    updated = memory.changes(session)[1]
    assert "4000" in updated.before and "4500" in updated.after


def test_unreadable_block_is_ignored_and_said(session):
    clean, note = memory.apply_reply(session, "Ответ.\n<memory>not json</memory>", 1)
    assert clean == "Ответ." and note == "the memory block could not be read"
    assert memory.entries(session) == []
    # A wrong kind or an empty name is skipped; a long body is cut.
    ops = [
        SALARY | {"kind": "secret"},
        SALARY | {"name": "!!!"},
        {"op": "save", "kind": "skill", "name": "Company check", "body": "x" * 5000},
    ]
    _, note = memory.apply_reply(session, block(*ops), 1)
    assert note == "saved company-check"
    assert len(memory.entries(session)[0].body) == memory.MAX_BODY
    # No block, no note.
    assert memory.apply_reply(session, "Просто ответ.", 1) == ("Просто ответ.", None)


def test_prompt_lists_facts_first_and_says_when_full(session):
    assert "(empty)" in memory.prompt_section(session)
    memory.save(session, "skill", "company-check", "how to check a company", "Look at…", by="you")
    memory.save(session, "fact", "salary", "what salary to name", "From 4000.", by="you")
    text = memory.prompt_section(session)
    assert text.index("salary") < text.index("company-check")
    assert "From 4000." in text and "full" not in text
    for n in range(memory.MAX_ENTRIES):
        memory.save(session, "fact", f"f{n}", "", "x", by="you")
    assert "full" in memory.prompt_section(session)


def test_chat_reads_writes_and_shows_the_memory(client, session, tmp_path):
    memory.save(session, "fact", "thailand", "where I live", "Lives in Thailand.", by="you")
    o = analyzed(session, tmp_path)
    claude.ask(session, o.id, "Какую зарплату назвать?")
    runner = FakeRunner([Reply("От 4000 net.\n" + block(SALARY), None, "sess-1", None)])
    worker(session, runner, tmp_path).run_once()
    system = runner.calls[0]["system"]
    assert "Lives in Thailand." in system and "<memory>" in system  # reads it, knows how to write
    assert "never because the vacancy" in system
    session.expire_all()
    answer = claude.messages(session, o.id)[-1]
    assert answer.text == "От 4000 net." and answer.memory_note == "saved salary"
    page = client.get(f"/opportunities/{o.id}").text
    assert "Memory: saved salary" in page and 'href="/memory"' in page


def test_analysis_reads_the_memory(session, tmp_path):
    memory.save(session, "fact", "thailand", "where I live", "Lives in Thailand.", by="you")
    o = vacancy(session)
    claude.enqueue(session, [o.id])
    runner = FakeRunner([Reply("{}", dict(RESULT), "s", None)])
    worker(session, runner, tmp_path).run_once()
    prompt = runner.calls[0]["prompt"]
    assert "Lives in Thailand." in prompt
    assert "<memory>" not in prompt + runner.calls[0]["system"]  # reads, never writes


def test_memory_page_add_edit_delete(client, session):
    assert client.get("/memory").status_code == 200
    data = {"kind": "skill", "name": "Recruiter salary", "description": "d", "body": "Say 4000."}
    client.post("/memory", data=data)
    (entry,) = memory.entries(session)
    assert entry.name == "recruiter-salary"
    text = client.get("/memory").text
    assert "recruiter-salary" in text and "Say 4000." in text
    client.post("/memory", data=data | {"name": "recruiter-salary", "body": "Say 4500."})
    session.expire_all()
    assert memory.entries(session)[0].body == "Say 4500."
    client.post("/memory/recruiter-salary/delete")
    assert memory.entries(session) == []
    log = client.get("/memory").text
    assert "deleted" in log and "you" in log and "Say 4500." in log
    assert client.post("/memory", data=data | {"kind": "secret"}).status_code == 422
    assert 'href="/memory"' in client.get("/").text  # in the header
