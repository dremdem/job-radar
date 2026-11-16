"""Description analysis with a local model (Ollama).

``ollama`` is a minimal client; ``analysis`` asks the model for the work mode,
residence limits and unknown technologies of a description and keeps only what the
text supports (quotes are checked by code). What the stored answers mean for the
queue is read by ``scoring_findings``. See docs/ai.md.

Usage:
    import job_radar.ai.local as local
"""
