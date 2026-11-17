"""A fake Claude runner, a ready answer, and helpers that build a vacancy and a worker.

Shared by the tests of several modules; see test_claude.py for how it is used.
"""

import job_radar.ai.claude.analysis as claude_analysis
import job_radar.ai.claude.runner as claude_runner

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
    """A Claude runner that returns the given replies in order and records prompts."""

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
    import ai_analysis_fakes

    return ai_analysis_fakes.add_vacancy(session, title=title, text=text)


def worker(session, runner, tmp_path, cv=True):
    import sqlalchemy.orm as sa_orm

    cv_path = tmp_path / "cv.md"
    if cv:
        cv_path.write_text("# Alex\nSenior Python developer, FastAPI, Django.")
    factory = sa_orm.sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    return claude_analysis.Worker(
        factory, runner, profile_path=tmp_path / "profile.yaml", cv_path=cv_path
    )


def analyzed(session, tmp_path):
    o = vacancy(session)
    claude_analysis.enqueue(session, [o.id])
    worker(
        session, FakeRunner([claude_runner.Reply("{}", dict(RESULT), "sess-1", None)]), tmp_path
    ).run_once()
    session.expire_all()
    return o
