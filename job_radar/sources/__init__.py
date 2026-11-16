"""Everything that brings vacancies and mail into the app.

For now it holds the code that several sources share: ``titles`` (the title and keyword
filters) and ``http`` (the User-Agent and the HTML-to-text reader). The sources themselves
move here in later steps of the layout plan (issue #31).

Usage:
    import job_radar.sources as sources
"""
