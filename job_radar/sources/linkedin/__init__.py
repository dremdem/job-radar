"""LinkedIn data, without automating the user's account.

``alerts`` reads job alert emails, ``rejections`` application-update emails, ``capture``
pages the user sent with the bookmarklet, ``public`` the opt-in public guest search and
job pages, and ``chrome`` the opt-in Chrome fill. The limits are in the LinkedIn rule of
CLAUDE.md. See docs/linkedin-alerts.md.

Usage:
    import job_radar.sources.linkedin as linkedin
"""
