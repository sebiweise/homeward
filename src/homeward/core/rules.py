"""Sync rules: fixed exclusions and, later, the bidirectional decision table."""

from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import PurePosixPath

DEFAULT_EXCLUDES: tuple[str, ...] = (
    "@eaDir",
    "#recycle",
    ".DS_Store",
    "Thumbs.db",
    ".SynologyWorkingDirectory",
)


def is_excluded(relative_path: str, extra_patterns: Iterable[str] = ()) -> bool:
    """Return True if any component of ``relative_path`` matches an exclusion pattern."""
    patterns = (*DEFAULT_EXCLUDES, *extra_patterns)
    parts = PurePosixPath(relative_path).parts
    return any(fnmatch(part, pattern) for part in parts for pattern in patterns)
