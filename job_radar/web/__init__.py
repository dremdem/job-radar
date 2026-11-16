"""The local web UI: FastAPI with Jinja templates and htmx.

``app`` builds the application (state, lifespan, routers), ``deps`` gives the routes the
database session and the objects kept on ``app.state``, ``templating`` holds the Jinja
environment with its filters and globals, ``context`` builds what the templates show
(filters, tabs, the queue, a row, a panel) and ``routes`` has one router per feature:
queue, claude, settings, jobs, capture and applications. ``job-radar serve`` listens on
127.0.0.1 by default. See docs/queue.md.

Usage:
    import job_radar.web as web
"""
