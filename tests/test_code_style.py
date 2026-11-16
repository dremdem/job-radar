"""Code style rules from CLAUDE.md that ruff cannot check (#21).

Every Python file of the package and the tests imports modules, not names (no
``from x import y`` except ``from __future__``), and keeps comments on their own line,
apart from tool directives. Every module of the ``job_radar`` package, except the
migrations, ends its docstring with a ``Usage:`` import line.
"""

import ast
import io
import pathlib
import re
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = sorted([*ROOT.joinpath("job_radar").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py")])
PACKAGE = [
    path
    for path in FILES
    if path.is_relative_to(ROOT / "job_radar")
    and not path.is_relative_to(ROOT / "job_radar" / "migrations")
]
# Comments the tools read at the end of a line.
DIRECTIVE = re.compile(r"#\s*(noqa|type:|pragma|fmt:|nosec)")


def name(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def module_path(path: pathlib.Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@pytest.mark.parametrize("path", FILES, ids=name)
def test_imports_modules_not_names(path):
    tree = ast.parse(path.read_text())
    found = [
        f"line {node.lineno}: from {node.module or '.'} import ..."
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    ]
    assert not found, "use `import x as alias` instead"


@pytest.mark.parametrize("path", FILES, ids=name)
def test_comments_are_on_their_own_line(path):
    source = path.read_text()
    lines = source.splitlines()
    found = [
        f"line {token.start[0]}: {token.string}"
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
        and lines[token.start[0] - 1][: token.start[1]].strip()
        and not DIRECTIVE.search(token.string)
    ]
    assert not found, "move the comment to its own line above the code"


@pytest.mark.parametrize("path", PACKAGE, ids=name)
def test_module_docstring_ends_with_an_import_line(path):
    docstring = ast.get_docstring(ast.parse(path.read_text())) or ""
    module = module_path(path)
    last = docstring.strip().splitlines()[-1].strip() if docstring.strip() else ""
    assert "Usage:" in docstring, "add a Usage: section with the import line"
    assert re.fullmatch(rf"import {re.escape(module)}( as \w+)?", last), last

