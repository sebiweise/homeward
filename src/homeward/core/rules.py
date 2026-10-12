"""Sync rules: fixed exclusions and the decision tables for every sync mode."""

from collections.abc import Iterable
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import PurePosixPath

from homeward.core.models import Change, SyncMode

TRASH_DIR = ".homeward-papierkorb"
TEMP_SUFFIX = ".homeward-tmp"

DEFAULT_EXCLUDES: tuple[str, ...] = (
    # Synology
    "@eaDir",
    "#recycle",
    "#snapshot",
    ".SynologyWorkingDirectory",
    # macOS and Windows metadata
    ".DS_Store",
    "._*",
    "Icon\r",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    "Thumbs.db",
    "desktop.ini",
    # Lock files of open Office / LibreOffice documents
    "~$*",
    ".~lock.*#",
    # Homeward's own trash and partial downloads
    TRASH_DIR,
    f"*{TEMP_SUFFIX}",
)


def is_excluded(relative_path: str, extra_patterns: Iterable[str] = ()) -> bool:
    """Return True if any component of ``relative_path`` matches an exclusion pattern."""
    patterns = (*DEFAULT_EXCLUDES, *extra_patterns)
    parts = PurePosixPath(relative_path).parts
    return any(fnmatch(part, pattern) for part in parts for pattern in patterns)


class Decision(StrEnum):
    """What the decision table says to do; the planner turns it into an Action."""

    NOTHING = "nothing"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_REMOTE = "delete-remote"
    DELETE_LOCAL = "delete-local"
    FORGET = "forget"
    IGNORE = "ignore"
    # Both sides hold a file at this path: keep it if the contents match, else ...
    SAME_OR_CONFLICT = "same-or-conflict"
    SAME_OR_UPLOAD = "same-or-upload"
    SAME_OR_DOWNLOAD = "same-or-download"


Row = tuple[Change, Change]
"""(NAS change, iCloud change) since the last sync."""

U, C, D, N, A = Change.UNCHANGED, Change.CHANGED, Change.DELETED, Change.NEW, Change.ABSENT

# Rows with a state record use UNCHANGED/CHANGED/DELETED, rows without use NEW/ABSENT.
ROWS: tuple[Row, ...] = (
    (U, U), (C, U), (U, C), (C, C), (D, U), (U, D), (D, C), (C, D), (D, D),
    (N, A), (A, N), (N, N),
)  # fmt: skip

BIDIRECTIONAL: dict[Row, Decision] = {
    (U, U): Decision.NOTHING,
    (C, U): Decision.UPLOAD,
    (U, C): Decision.DOWNLOAD,
    (C, C): Decision.SAME_OR_CONFLICT,  # both changed: keep both
    (D, U): Decision.DELETE_REMOTE,  # into the iCloud trash
    (U, D): Decision.DELETE_LOCAL,  # into the package trash
    (D, C): Decision.DOWNLOAD,  # the changed version wins and is restored
    (C, D): Decision.UPLOAD,  # the changed version wins and is restored
    (D, D): Decision.FORGET,
    (N, A): Decision.UPLOAD,
    (A, N): Decision.DOWNLOAD,
    (N, N): Decision.SAME_OR_CONFLICT,  # same content: just remember it
}

# One-way modes: the source side wins, the target's own edits are overwritten
# (the old target version goes to the trash), and files that only ever existed
# on the target are left alone.
UPLOAD: dict[Row, Decision] = {
    (U, U): Decision.NOTHING,
    (C, U): Decision.UPLOAD,
    (U, C): Decision.UPLOAD,
    (C, C): Decision.SAME_OR_UPLOAD,
    (D, U): Decision.DELETE_REMOTE,
    (U, D): Decision.UPLOAD,
    (D, C): Decision.DELETE_REMOTE,
    (C, D): Decision.UPLOAD,
    (D, D): Decision.FORGET,
    (N, A): Decision.UPLOAD,
    (A, N): Decision.IGNORE,
    (N, N): Decision.SAME_OR_UPLOAD,
}

DOWNLOAD: dict[Row, Decision] = {
    (U, U): Decision.NOTHING,
    (C, U): Decision.DOWNLOAD,
    (U, C): Decision.DOWNLOAD,
    (C, C): Decision.SAME_OR_DOWNLOAD,
    (D, U): Decision.DOWNLOAD,
    (U, D): Decision.DELETE_LOCAL,
    (C, D): Decision.DELETE_LOCAL,
    (D, C): Decision.DOWNLOAD,
    (D, D): Decision.FORGET,
    (N, A): Decision.IGNORE,
    (A, N): Decision.DOWNLOAD,
    (N, N): Decision.SAME_OR_DOWNLOAD,
}

TABLES: dict[SyncMode, dict[Row, Decision]] = {
    SyncMode.BIDIRECTIONAL: BIDIRECTIONAL,
    SyncMode.UPLOAD: UPLOAD,
    SyncMode.DOWNLOAD: DOWNLOAD,
}


def decide(mode: SyncMode, local: Change, remote: Change) -> Decision:
    """Look up the decision for one file."""
    return TABLES[mode][(local, remote)]
