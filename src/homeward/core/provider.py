"""The interface every storage side implements (NAS folder, iCloud Drive, fake).

It lives in the core so planner and executor can depend on it without importing
any adapter; ``homeward.providers.base`` re-exports it for adapter authors.
"""

from typing import BinaryIO, Protocol

from homeward.core.models import FileMeta


class Provider(Protocol):
    """A tree of files addressed by relative POSIX paths."""

    name: str
    # Optional: a provider whose names ignore upper/lower case (iCloud Drive) sets
    # ``case_insensitive = True``; the planner then avoids uploads that would collide.
    # Optional: ``file_identity(version) -> str`` returns the part of a version token
    # that survives a rename or move, so the planner can recognise renames there.
    # Optional: ``root_exists() -> bool`` tells, after ``list_files``, whether the
    # root folder exists at all, so a missing folder is not taken for an empty one.

    def list_files(self) -> dict[str, FileMeta]:
        """Return every file below the root, keyed by relative path."""
        ...

    def open_read(self, path: str) -> BinaryIO:
        """Open a file for streaming reads. The caller closes it."""
        ...

    def write(self, path: str, data: BinaryIO, mtime: float) -> FileMeta:
        """Create a file that does not exist yet, creating parent folders as needed.

        Implementations must never leave a partial file at ``path``.
        """
        ...

    def delete(self, path: str) -> None:
        """Move a file to a recoverable place (package trash, iCloud trash)."""
        ...

    def move(self, path: str, new_path: str) -> FileMeta:
        """Rename a file within this side. ``new_path`` must not exist."""
        ...

    def sha256(self, path: str) -> str:
        """Return the SHA-256 hex digest of a file's content."""
        ...
