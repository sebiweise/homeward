"""Core data types shared by planner, executor and providers."""

from dataclasses import dataclass, field
from enum import StrEnum


class SyncMode(StrEnum):
    """Direction of a Drive sync pair."""

    UPLOAD = "upload"
    DOWNLOAD = "download"
    BIDIRECTIONAL = "bidirectional"


@dataclass(frozen=True, slots=True)
class FileMeta:
    """A file as one side currently sees it.

    ``version`` is an opaque, side-specific change token (size + mtime on the NAS,
    the etag in iCloud). Comparing it with the token stored after the last sync tells
    whether that side changed. ``sha256`` is only set when the side knows it cheaply.
    """

    path: str
    size: int
    mtime: float
    version: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FileRecord:
    """Last common state of a file, written after each successful transfer."""

    path: str
    local_version: str
    remote_version: str
    size: int
    sha256: str | None
    synced_at: float


class Change(StrEnum):
    """What happened to a file on one side since the last sync."""

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    DELETED = "deleted"
    NEW = "new"
    ABSENT = "absent"


class ActionKind(StrEnum):
    """A concrete step the executor performs."""

    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_REMOTE = "delete-remote"
    DELETE_LOCAL = "delete-local"
    CONFLICT = "conflict"
    RECORD = "record"
    FORGET = "forget"
    SKIP = "skip"


DELETE_KINDS = frozenset({ActionKind.DELETE_LOCAL, ActionKind.DELETE_REMOTE})


@dataclass(frozen=True, slots=True)
class Action:
    """One planned step for one path.

    ``overwrite`` means the target already holds a file that must be moved to the
    trash first. ``verify_content`` means neither side knew a checksum at planning
    time: the executor compares contents first and only records the file if they match.
    ``renamed_to`` marks a delete whose content reappears under that new path on the
    same side, i.e. one half of a rename or move (the other half is a transfer).
    """

    kind: ActionKind
    path: str
    reason: str
    local: FileMeta | None = None
    remote: FileMeta | None = None
    overwrite: bool = False
    verify_content: bool = False
    conflict_path: str | None = None
    renamed_to: str | None = None


@dataclass(slots=True)
class Plan:
    """Everything a run would do for one sync pair."""

    pair: str
    mode: SyncMode
    first_run: bool
    tracked: int
    actions: list[Action] = field(default_factory=list)
    blocked: str | None = None

    @property
    def deletes(self) -> int:
        """Deletions the delete brake counts: renames and moves are not counted."""
        return sum(
            1 for action in self.actions if action.kind in DELETE_KINDS and not action.renamed_to
        )

    @property
    def renames(self) -> int:
        return sum(1 for action in self.actions if action.renamed_to)

    def counts(self) -> dict[ActionKind, int]:
        result: dict[ActionKind, int] = {}
        for action in self.actions:
            result[action.kind] = result.get(action.kind, 0) + 1
        return result
