"""The mail port: what code that reads stored emails needs from the mail provider.

``FetchBody`` is the callable that returns the (html, text) of one message by id; it is
passed to the importers that look at emails stored without a body (LinkedIn rejections,
applications, hh.ru digests and notifications). The Gmail client implements it
(``gmail.client.body_fetcher``), tests pass a fake. ``MessageGone`` is what it raises when
the message was deleted from Gmail for good, and ``GONE_EVENT`` is the ``detected_event``
stored on such an email so it is not fetched again. See docs/gmail.md.

Usage:
    import job_radar.mail as mail
"""

import collections.abc as cabc

# the message was deleted from Gmail for good; its body can't be read
GONE_EVENT = "gone"

# Returns (html, text) of a message; used for emails stored without a body.
# Raises MessageGone when Gmail no longer has the message (404).
FetchBody = cabc.Callable[[str], tuple[str | None, str | None]]


class MessageGone(Exception):
    """The message is no longer in Gmail (deleted from Trash), so its body can't be fetched."""
