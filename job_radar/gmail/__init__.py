"""Read-only Gmail import.

``auth`` runs the one-time consent with the read-only scope, ``client`` wraps the Gmail
API with pacing and rate-limit retries, ``queries`` lists the searches that are synced,
``parse`` turns an API message into plain fields and ``sync`` stores new messages as
``EmailMessage`` rows. Mail is never sent, changed or deleted. See docs/gmail.md.

Usage:
    import job_radar.gmail as gmail
"""
