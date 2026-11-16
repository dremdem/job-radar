"""Background work: the sync pipeline and the state of running jobs.

``pipeline`` holds every sync step in order; the CLI and the web UI run the same
list. ``sync_job`` and ``description_job`` keep the state of one background run
for the status lines. See docs/queue.md#sync.

Usage:
    import job_radar.jobs as jobs
"""
