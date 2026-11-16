"""Job Radar: local-first assistant for reviewing job opportunities.

The package collects vacancies from mail and public job boards, scores them, keeps their
history in SQLite and serves a review queue in the browser. Sources live in their own
modules and subpackages (``gmail``, ``linkedin``, ``hh``, ``telegram``, ``fdetimes`` and
others); ``pipeline`` runs them all, ``web`` is the UI, ``cli`` the command line.
See README.md and docs/architecture.md.

Usage:
    import job_radar
"""
