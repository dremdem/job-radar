"""Ctrl+Enter (⌘+Enter on a Mac) sends the Claude chat message; Enter stays a new line (#227).

The keys are handled in the browser, so the second test runs the real page in headless
Chromium. It needs Playwright and its Chromium, which the project does not install:
    uv run --with playwright pytest tests/web/test_chat_keys.py
Without them that test is skipped, and the markup test still runs.
"""

import browser_helpers
import claude_fakes
import pytest


def test_the_chat_says_how_to_send(client, session, tmp_path):
    o = claude_fakes.analyzed(session, tmp_path)
    page = client.get(f"/opportunities/{o.id}").text
    assert "Ctrl+Enter" in page and "⌘+Enter" in page


def test_ctrl_enter_sends_once_and_enter_is_a_new_line(client, session, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not browser_helpers.CHROME.exists():
        pytest.skip(f"no Chromium at {browser_helpers.CHROME}")
    o = claude_fakes.analyzed(session, tmp_path)
    html = client.get(f"/opportunities/{o.id}").text

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(browser_helpers.CHROME))
        page = browser.new_page()
        # no LinkedIn, no CDN
        page.route("**/*", lambda route, request: route.abort())
        page.set_content(html)
        # Without htmx (blocked above) count the submits instead of sending them.
        page.evaluate(
            """() => { window.sent = [];
              const form = document.querySelector('#claude-section form');
              form.addEventListener('submit', e => { e.preventDefault();
                window.sent.push(form.querySelector('textarea[name=text]').value); }); }"""
        )
        box = page.locator("#claude-section textarea[name=text]")
        box.click()
        box.type("first line")
        # a new line, not a send
        box.press("Enter")
        box.type("second line")
        assert page.evaluate("window.sent") == []
        assert box.input_value() == "first line\nsecond line"

        box.press("Control+Enter")
        assert page.evaluate("window.sent") == ["first line\nsecond line"]
        # Ctrl+Enter adds no line
        assert box.input_value() == "first line\nsecond line"

        # ⌘+Enter on a Mac
        box.press("Meta+Enter")
        assert len(page.evaluate("window.sent")) == 2

        # While htmx is still sending, another Ctrl+Enter does not send twice.
        form = "document.querySelector('#claude-section form')"
        busy = f"on => {form}.classList.toggle('htmx-request', on)"
        page.evaluate(busy, True)
        box.press("Control+Enter")
        assert len(page.evaluate("window.sent")) == 2

        # An empty box is not sent (the field is required).
        page.evaluate(busy, False)
        box.fill("")
        box.press("Control+Enter")
        assert len(page.evaluate("window.sent")) == 2
        browser.close()
