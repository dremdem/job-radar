"""Everything that brings vacancies and mail into the app.

One package per source with its own protocol: ``gmail`` (read-only mail import),
``linkedin`` (alert emails, the public search, the bookmarklet capture and the Chrome
fill), ``hh`` (hh.ru digests, notifications and the bookmarklet capture), ``telegram``
(the channel allowlist and the posts read through telegram-mcp) and ``boards`` (job boards
with a public API or feed). ``manual`` adds vacancies and events by hand. ``titles`` and
``http`` hold what several sources share (title filters, User-Agent, HTML reader) and
``capture_read.js`` is the page reader used by the bookmarklet and the Chrome fill. A
source stores vacancies and then calls the rules; nothing below it imports a source. See
docs/architecture.md.

Usage:
    import job_radar.sources as sources
"""
