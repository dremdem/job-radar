"""A fake local model, its answers and a vacancy with a description.

Shared by the tests of several modules; see test_ai_analysis.py for how it is used.
"""

import typing as t

import job_radar.dedup as dedup
import job_radar.models as models

UK_ONLY = (
    "We build payments.\nThe applicant must be based in the UK and able to work fully remotely.\n"
    "Python and FastAPI."
)


class FakeModel:
    """A local model that returns the given answers in order."""

    model = "fake:1b"

    def __init__(self, answers: list[dict[str, t.Any] | Exception]):
        self.answers = answers
        self.prompts: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, t.Any]) -> dict[str, t.Any]:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(mode="remote", mode_quote="", residence=(), residence_quote="") -> dict[str, t.Any]:
    return {
        "work_mode": mode,
        "work_mode_quote": mode_quote,
        "residence": list(residence),
        "residence_quote": residence_quote,
    }


UK_ANSWER = answer(
    "remote",
    "able to work fully remotely",
    ["UK"],
    "The applicant must be based in the UK and able to work fully remotely.",
)


def add_vacancy(session, title="Python Developer", text=UK_ONLY, state=models.OpportunityState.NEW):
    o = models.Opportunity(
        title=title, company=dedup.get_or_create_company(session, "Acme"), score=8.0
    )
    o.state = state
    session.add(o)
    session.flush()
    record = dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_CAPTURE,
        external_id=str(o.id),
        url=None,
        opportunity=o,
    )
    if text:
        dedup.add_snapshot(session, record, text)
    session.commit()
    return o
