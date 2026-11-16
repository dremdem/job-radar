"""Vacancies from allowlisted Telegram channels, read via telegram-mcp.

``sources`` loads the channel allowlist, ``mcp_reader`` reads posts through the
telegram-mcp server (only ``read_messages``), ``sync`` stores posts and turns relevant
ones into vacancies, ``posts`` parses a single-vacancy post and ``digest`` follows the
links of digest posts. Telegram is read-only. See docs/telegram.md.

Usage:
    import job_radar.sources.telegram as telegram
"""
