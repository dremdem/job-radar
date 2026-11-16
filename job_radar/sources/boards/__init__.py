"""Job boards with a public API or feed.

One module per board (``himalayas``, ``hackernews``, ``remoteok``, ``jobicy``,
``fdetimes``) and ``feeds`` for the small boards that share one sync step. Each
has a client behind a ``Protocol``, so tests pass a fake. See docs/boards.md.

Usage:
    import job_radar.sources.boards as boards
"""
