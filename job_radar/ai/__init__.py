"""Description analysis with a local model (Ollama).

``ollama`` is a minimal client; ``analysis`` asks the model for the work mode, residence
limits and unknown technologies of a description and keeps only what the text supports
(quotes are checked by code). Results feed the queue's risks and auto-ignore. See
docs/ai.md.

Usage:
    import job_radar.ai as ai
"""
