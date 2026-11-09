"""Moving files aside instead of deleting them: into the share's Synology recycle bin
when it covers the file, else into Homeward's own trash ``<root>/.homeward-papierkorb/<date>/``.

Both keep the file's relative path. A name that is already taken gets a number the
way DSM numbers it (``IMG_1 (2).HEIC``), and nothing is ever overwritten.
"""

import errno
import logging
import os
import shutil
import stat
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath

from homeward.core.files import Confined, SymlinkInPath, rename_noreplace
from homeward.core.rules import TRASH_DIR

log = logging.getLogger(__name__)


def numbered(path: PurePosixPath, number: int) -> PurePosixPath:
    """``a/b.txt`` -> ``a/b (2).txt``."""
    return path.with_name(f"{path.stem} ({number}){path.suffix}")


def move_aside(
    source: Path, folder: Path, relative: str, *, inside: Confined | None = None
) -> Path:
    """Move ``source`` to ``folder/relative``, numbered if that name is taken. With
    ``inside`` (a pinned folder that contains ``folder``), the target folder must lie
    inside it and no folder down to the target may be a symlink
    (``core.files.Confined``)."""
    wanted = PurePosixPath(relative)
    target, number = folder / relative, 2
    if inside is not None:
        inside.check(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    if inside is not None:
        inside.check(target.parent)
    while True:
        try:
            rename_noreplace(source, target)
            return target
        except FileExistsError:
            target = folder / numbered(wanted, number)
            number += 1


def _check_real_folder(folder: Path, *, missing_ok: bool = False) -> None:
    """Raise ``SymlinkInPath`` unless ``folder`` is a folder and not a symlink."""
    try:
        mode = os.lstat(folder).st_mode
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if not stat.S_ISDIR(mode):
        raise SymlinkInPath(errno.ENOTDIR, "not a folder (or a symlink)", str(folder))


class Trash:
    """Where files below ``root`` go when they are deleted or replaced."""

    def __init__(
        self,
        root: Path,
        *,
        recycle_bin: Path | None = None,
        trash_root: Path | None = None,
        today: Callable[[], date] = date.today,
        confined: Confined | None = None,
    ) -> None:
        self.root = root
        self.recycle_bin = recycle_bin
        self.trash_root = trash_root or root / TRASH_DIR
        self._today = today
        # Pinned now: a share user may swap any of them for a symlink during the run.
        self._root = confined or Confined(root)
        self._bin = Confined(recycle_bin) if recycle_bin is not None else None
        self._trash = Confined(self.trash_root)

    def move(self, source: Path, relative: str) -> Path:
        """Move ``source`` (the file at ``relative`` below the root) aside."""
        if source.is_relative_to(self.root):
            self._root.check(source.parent)  # never move a file from elsewhere
        if self.recycle_bin is not None and self._bin is not None:
            try:
                _check_real_folder(self.recycle_bin)
                in_bin = source.relative_to(self.recycle_bin.parent).as_posix()
                return move_aside(source, self.recycle_bin, in_bin, inside=self._bin)
            except (OSError, ValueError) as exc:  # e.g. no write permission in #recycle
                log.info("recycle bin not usable (%s), using Homeward's own trash", exc)
        self._check_trash_root()
        day = self.trash_root / self._today().isoformat()
        return move_aside(source, day, relative, inside=self._trash)

    def _check_trash_root(self) -> None:
        """Homeward's own trash must be a real folder below the root (if it exists):
        a share user could replace it with a symlink to send deletes elsewhere."""
        _check_real_folder(self.trash_root, missing_ok=True)
        self._trash.check(self.trash_root)
        if self.trash_root.is_relative_to(self.root):
            self._root.check(self.trash_root)

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
