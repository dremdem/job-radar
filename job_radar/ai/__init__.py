"""Models that read a vacancy: the local model and Claude.

``local`` extracts work mode and residence with a local model (Ollama), ``claude`` runs
the Claude analysis and the chat, and ``skills`` builds the skills gap report from
Claude's answers. See docs/ai.md and docs/claude.md.

Usage:
    import job_radar.ai as ai
"""
