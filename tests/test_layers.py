"""The packages of ``job_radar`` form layers: a module imports only from layers below.

The order is in ``LAYERS``, highest first; names on one line do not import each other.
An import inside a function counts too. See docs/architecture.md#layers.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "job_radar"
LAYERS = [
    ("web", "cli"),
    ("jobs", "demo"),
    ("sources",),
    ("ai", "history"),
    ("review",),
    ("scoring",),
    ("profile",),
    ("dedup",),
    ("models", "db", "config", "mail"),
]
RANK = {name: rank for rank, names in enumerate(LAYERS) for name in names}
FILES = sorted(
    path for path in PACKAGE.rglob("*.py") if not path.is_relative_to(PACKAGE / "migrations")
)


def layer(module: str) -> str | None:
    """``job_radar.sources.gmail.client`` is in ``sources``; ``job_radar`` itself in none."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "job_radar" else None


def name(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def test_every_top_level_name_has_a_layer():
    found = {path.relative_to(PACKAGE).parts[0].removesuffix(".py") for path in FILES}
    assert found - {"__init__"} == set(RANK)


@pytest.mark.parametrize("path", FILES, ids=name)
def test_imports_only_from_lower_layers(path):
    own = layer(".".join(path.relative_to(ROOT).with_suffix("").parts))
    if own is None:
        return
    found = [
        f"line {node.lineno}: {alias.name}"
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Import)
        for alias in node.names
        if (other := layer(alias.name)) and other != own and RANK[other] <= RANK[own]
    ]
    assert not found, f"{own} must not import from a layer above or beside it"
