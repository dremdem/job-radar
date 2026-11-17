"""Where the tests find files of the repository: its root and the recorded fixtures.

Test modules live in folders of different depth, so they take the paths from here
instead of counting ``parent``s from ``__file__``.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
