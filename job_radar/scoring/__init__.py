"""Checks on a vacancy that need no model and no decision of the user.

``rules`` scores the title and the location, ``description`` checks the stored
description for Python, ``language`` guesses the language of the vacancy and
``findings`` reads what the local model stored. Each check returns score effects
with reasons. See docs/queue.md.

Usage:
    import job_radar.scoring as scoring
"""
