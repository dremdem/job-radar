"""A vacancy for the tests of what the user adds by hand.

Shared by the tests of several modules; see test_manual.py for how it is used.
"""

import job_radar.dedup as dedup
import job_radar.models as models


def vacancy(session, title="Python Developer", company="Acme"):
    o = models.Opportunity(title=title, company=dedup.get_or_create_company(session, company))
    session.add(o)
    session.commit()
    return o
