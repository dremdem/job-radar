"""Turn a real email body into a test fixture without personal data.

Keeps only LinkedIn job ids in links, drops images, styles and tracking, and replaces
the recipient's name, headline and addresses. Usage from code: sanitize_html / sanitize_text.
Always check the result by eye and run the fixture privacy tests before committing.
"""

import re

from bs4 import BeautifulSoup, Comment

NAMES = r"Vladimir|Владимир|Yakovenko|Яковенко|Игоревич|Iakovenko|IAKOVENKO"


def _common(text: str) -> str:
    text = re.sub(r"intended for [^<\n]+", "intended for Alex Example.", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", text)
    return re.sub(NAMES, "Alex", text)


def sanitize_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr not in ("href", "alt", "colspan", "rowspan"):
                del tag[attr]
        if tag.has_attr("href"):
            m = re.search(r"/(comm/)?jobs/view/(\d+)", tag["href"])
            hh = re.search(r"hh\.ru/vacancy/(\d+)", tag["href"])
            if m:
                tag["href"] = (
                    f"https://www.linkedin.com/{m.group(1) or ''}jobs/view/{m.group(2)}/"
                    "?trackingId=REDACTED&otpToken=REDACTED"
                )
            else:
                tag["href"] = f"https://hh.ru/vacancy/{hh.group(1)}?from=REDACTED" if hh else "#"
        if tag.name == "img":
            tag["src"] = ""
    return re.sub(r"\n\s*\n+", "\n", _common(str(soup)))


def sanitize_text(text: str) -> str:
    return _common(re.sub(r"https?://\S+", "https://example.com/", text))
