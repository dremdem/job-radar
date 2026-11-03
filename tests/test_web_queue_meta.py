"""The company · location line never takes the screen, whatever the data (#267)."""

from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity

RUN_ON = "100% Remote (Global) · Full-Time MixRank processes petabytes of data " * 20


def test_long_location_is_capped_with_the_full_text_in_the_tooltip(client, session):
    o = Opportunity(title="Software Engineers", location=RUN_ON.strip(),
                    company=get_or_create_company(session, "MixRank (YC S11)"))  # fmt: skip
    session.add(o)
    session.commit()
    row = client.get("/").text
    panel = client.get(f"/opportunities/{o.id}/panel").text
    for text, cls in ((row, "item-meta"), (panel, "detail-meta")):
        start = text.index(f'class="{cls}"')
        tag = text[text.rindex("<", 0, start) : text.index(">", start)]
        assert 'title="MixRank (YC S11) · 100% Remote (Global)' in tag, cls
    css = client.get("/").text
    for cls in (".item-meta", ".detail-meta"):
        rule = css[css.index(cls + " {") :].split("}")[0]
        assert "-webkit-line-clamp: 2" in rule and "overflow: hidden" in rule, cls
