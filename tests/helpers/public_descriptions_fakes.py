"""A fake HTTP client for LinkedIn's public job page and a vacancy without a description.

Shared by the tests of several modules; see test_public_descriptions.py for how it is used.
"""

import paths

import job_radar.dedup as dedup
import job_radar.models as models

POSTING = (paths.FIXTURES / "linkedin_public" / "job_posting.html").read_text()


class FakeHttp:
    """Answers every request with the same status and page."""

    def __init__(self, response=(200, "u", POSTING)):
        self.response, self.urls = response, []

    def __call__(self, url):
        self.urls.append(url)
        return self.response


def vacancy(session, job_id, score, state=models.OpportunityState.NEW, title="Backend Engineer"):
    o = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, f"Co {job_id}"),
        state=state,
        score=score,
    )
    session.add(o)
    session.flush()
    dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_ALERT,
        external_id=job_id,
        url=None,
        opportunity=o,
    )
    session.commit()
    return o
