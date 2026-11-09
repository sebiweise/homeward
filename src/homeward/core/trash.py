"""Moving files aside instead of deleting them: into the share's Synology recycle bin
when it covers the file, else into Homeward's own trash ``<root>/.homeward-papierkorb/<date>/``.

Both keep the file's relative path. A name that is already taken gets a number the
way DSM numbers it (``IMG_1 (2).HEIC``), and nothing is ever overwritten.
"""

import logging
import os
import shutil
import stat
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath

from homeward.core.files import SymlinkInPath, check_no_symlinks, rename_noreplace
from homeward.core.rules import TRASH_DIR

log = logging.getLogger(__name__)


def numbered(path: PurePosixPath, number: int) -> PurePosixPath:
    """``a/b.txt`` -> ``a/b (2).txt``."""
    return path.with_name(f"{path.stem} ({number}){path.suffix}")


def move_aside(source: Path, folder: Path, relative: str, *, root: Path | None = None) -> Path:
    """Move ``source`` to ``folder/relative``, numbered if that name is taken. With
    ``root`` (a folder that contains ``folder``), no folder from ``root`` down to the
    target may be a symlink (``core.files.check_no_symlinks``)."""
    wanted = PurePosixPath(relative)
    target, number = folder / relative, 2
    if root is not None:
        check_no_symlinks(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    if root is not None:
        check_no_symlinks(root, target.parent)
    while True:
        try:
            rename_noreplace(source, target)
            return target
        except FileExistsError:
            target = folder / numbered(wanted, number)
            number += 1


class Trash:
    """Where files below ``root`` go when they are deleted or replaced."""

    def __init__(
        self,
        root: Path,
        *,
        recycle_bin: Path | None = None,
        trash_root: Path | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.root = root
        self.recycle_bin = recycle_bin
        self.trash_root = trash_root or root / TRASH_DIR
        self._today = today

    def move(self, source: Path, relative: str) -> Path:
        """Move ``source`` (the file at ``relative`` below the root) aside."""
        if source.is_relative_to(self.root):
            check_no_symlinks(self.root, source.parent)  # never move a file from elsewhere
        if self.recycle_bin is not None:
            try:
                in_bin = source.relative_to(self.recycle_bin.parent).as_posix()
                return move_aside(source, self.recycle_bin, in_bin, root=self.recycle_bin)
            except (OSError, ValueError) as exc:  # e.g. no write permission in #recycle
                log.info("recycle bin not usable (%s), using Homeward's own trash", exc)
        self._check_trash_root()
        day = self.trash_root / self._today().isoformat()
        return move_aside(source, day, relative, root=self.trash_root)

    def _check_trash_root(self) -> None:
        """Homeward's own trash must be a real folder below the root (if it exists):
        a share user could replace it with a symlink to send deletes elsewhere."""
        try:
            mode = os.lstat(self.trash_root).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and not stat.S_ISDIR(mode):
            raise SymlinkInPath(0, "trash is not a folder", str(self.trash_root))
        if self.trash_root.is_relative_to(self.root):
            check_no_symlinks(self.root, self.trash_root)

    def purge(self, max_age_days: int) -> int:
        """Delete day folders in Homeward's own trash older than ``max_age_days``.

        Never touches the Synology recycle bin: DSM empties it on its own schedule.
        Returns the number of day folders removed.
        """
        if max_age_days <= 0 or not self.trash_root.is_dir():
            return 0
        try:
            self._check_trash_root()
        except OSError as exc:
            log.warning("not emptying the trash %s: %s", self.trash_root, exc)
            return 0
        today = self._today()
        removed = 0
        for entry in self.trash_root.iterdir():
            try:
                day = date.fromisoformat(entry.name)
            except ValueError:
                continue
            if entry.is_dir() and not entry.is_symlink() and (today - day).days > max_age_days:
                shutil.rmtree(entry)
                removed += 1
        return removed
