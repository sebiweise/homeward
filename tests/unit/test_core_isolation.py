"""The core must not depend on DSM or pyicloud code (see CLAUDE.md)."""

import ast
from pathlib import Path

import homeward.core

FORBIDDEN = ("pyicloud", "homeward.providers", "homeward.api", "homeward.daemon", "homeward.auth")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_has_no_forbidden_imports() -> None:
    core_dir = Path(homeward.core.__file__).parent
    for source in core_dir.rglob("*.py"):
        for name in _imports(source):
            assert not name.startswith(FORBIDDEN), f"{source.name} imports {name}"
