"""The review queue: what leaves To review by itself and what the user decides.

``auto_ignore`` holds the auto-ignore rules, ``applied_duplicates`` folds a repost
of a vacancy already applied to, ``notes`` stores ignore reasons and free notes and
``suggestions`` offers ready-made reasons. See docs/queue.md.

Usage:
    import job_radar.review as review
"""
