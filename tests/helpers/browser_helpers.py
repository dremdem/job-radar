"""Where the headless Chromium of the browser tests lives.

Shared by the tests of several modules; see test_chat_keys.py for how it is used.
"""

import pathlib

CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1228/chrome-linux64/chrome"
