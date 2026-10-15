"""Local NAS file system provider."""

import hashlib
import os
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from homeward.core.files import rename_noreplace, temp_name
from homeward.core.models import FileMeta
from homeward.core.rules import TRASH_DIR, is_excluded
from homeward.providers.base import Provider

_CHUNK = 1024 * 1024
RECYCLE_DIR = "#recycle"


def find_recycle_bin(root: Path) -> Path | None:
    """Return the Synology recycle bin (``#recycle``) that covers ``root``, if enabled.

    DSM keeps it at the top of a shared folder (``/volume1/docs/#recycle``) and, for
    the homes share, in each home folder (``/volume1/homes/alice/#recycle``). It
    only exists while the recycle bin is enabled for that share.
    """
    for folder in (root, *root.parents):
        if len(folder.parts) <= 2:  # "/" and "/volume1" are never shares
            break
        candidate = folder / RECYCLE_DIR
        if candidate.is_dir():
            return candidate
    return None


class NasProvider(Provider):
    """Files below ``root``.

    Deleted files move to the share's Synology recycle bin when ``recycle_bin`` is
    given (same layout DSM uses, so DSM's retention rules apply), otherwise or if
    that fails to ``<root>/.homeward-papierkorb/<date>/``.

    Writes go to a temporary file next to the target and are renamed into place
    without replacing (see ``core.files``), so a crash never leaves a partial file
    and an existing file is never clobbered.
    """

    def __init__(
        self,
        root: Path,
        *,
        name: str = "nas",
        trash_root: Path | None = None,
        recycle_bin: Path | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.name = name
        self.root = root
        self.trash_root = trash_root or root / TRASH_DIR
        self.recycle_bin = recycle_bin
        self._today = today

    def root_exists(self) -> bool:
        return self.root.is_dir()

    def list_files(self) -> dict[str, FileMeta]:
        files: dict[str, FileMeta] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root)
            dirnames[:] = [d for d in dirnames if not is_excluded(str(rel_dir / d))]
            for filename in filenames:
                rel = (rel_dir / filename).as_posix()
                full = Path(dirpath, filename)
                if is_excluded(rel) or full.is_symlink() or not full.is_file():
                    continue
                files[rel] = self._meta(rel, full)
        return files

    def open_read(self, path: str) -> BinaryIO:
        return self._full(path).open("rb")

    def write(self, path: str, data: BinaryIO, mtime: float) -> FileMeta:
        target = self._full(path)
        if target.exists():
            raise FileExistsError(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_name(target)
        try:
            with tmp.open("xb") as out:
                shutil.copyfileobj(data, out, _CHUNK)
                out.flush()
                os.fsync(out.fileno())
            os.utime(tmp, (mtime, mtime))
            rename_noreplace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return self._meta(path, target)

    def delete(self, path: str) -> None:
        source = self._full(path)
        if self.recycle_bin is not None:
            try:
                relative = source.relative_to(self.recycle_bin.parent).as_posix()
                _move_aside(source, self.recycle_bin, relative)
                return
            except (OSError, ValueError):
                pass  # e.g. no write permission in #recycle: use our own trash
        _move_aside(source, self.trash_root / self._today().isoformat(), path)

    def purge_trash(self, max_age_days: int) -> int:
        """Delete day folders in the package trash older than ``max_age_days``.

        Only touches ``<date>`` folders in Homeward's own trash, never the Synology
        recycle bin. Returns the number of day folders removed.
        """
        if max_age_days <= 0 or not self.trash_root.is_dir():
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

    def move(self, path: str, new_path: str) -> FileMeta:
        source, target = self._full(path), self._full(new_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rename_noreplace(source, target)
        return self._meta(new_path, target)

    def sha256(self, path: str) -> str:
        digest = hashlib.sha256()
        with self._full(path).open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()

    def _full(self, path: str) -> Path:
        rel = PurePosixPath(path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(f"invalid relative path: {path!r}")
        return self.root.joinpath(*rel.parts)

    @staticmethod
    def _meta(path: str, full: Path) -> FileMeta:
        st = full.stat()
        return FileMeta(path, st.st_size, st.st_mtime, f"{st.st_size}:{st.st_mtime_ns}")


def _move_aside(source: Path, folder: Path, relative: str) -> None:
    """Move ``source`` to ``folder/relative``, numbering the name if that is taken."""
    target = folder / relative
    number = 2
    while target.exists():
        target = folder / f"{relative} ({number})"
        number += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
