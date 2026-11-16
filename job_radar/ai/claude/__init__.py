"""Deep analysis and chat with Claude through Claude Code in print mode.

``runner`` runs ``claude -p`` on the user's subscription, ``prompt`` builds what Claude is
told, ``analysis`` keeps the queue of analyses and the background workers, and
``memory`` holds the facts and skills Claude keeps between chats. See docs/claude.md.

Usage:
    import job_radar.ai.claude as claude
"""
