"""The local web UI: FastAPI with Jinja templates and htmx.

``app`` builds the application and its pages, ``sync_job`` and ``description_job`` run the
Sync and Descriptions buttons in the background. ``job-radar serve`` listens on 127.0.0.1
by default. See docs/queue.md.

Usage:
    import job_radar.web as web
"""
