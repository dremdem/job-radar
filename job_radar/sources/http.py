"""What the HTTP sources share: the User-Agent and the reader of HTML descriptions.

``USER_AGENT`` is sent with every request to a public job board. ``html_text`` turns the
HTML description of a vacancy into plain text, keeping paragraphs, bullets and the full
address of links. See docs/himalayas.md.

Usage:
    import job_radar.sources.http as sources_http
"""

import re

import bs4

USER_AGENT = "job-radar (personal job search, one run a day)"
_BLOCKS = ["p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"]


def html_text(html: str) -> str:
    """Plain text of an HTML description, with line breaks and bullets kept."""
    soup = bs4.BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        # Hacker News shows a long link cut short with "..."; keep the whole address (#243).
        shown, href = a.get_text(), str(a["href"])
        if shown.endswith("...") and href.startswith(shown[:-3]):
            a.string = href
    for tag in soup.find_all(_BLOCKS):
        if tag.name == "li":
            tag.insert_before("• ")
        tag.insert_after("\n")
    text = re.sub(r"[ \t]+\n", "\n", soup.get_text())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
