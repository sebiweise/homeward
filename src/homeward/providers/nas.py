"""Local NAS file system provider."""

import hashlib
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from homeward.core.models import FileMeta
from homeward.core.rules import TEMP_SUFFIX, TRASH_DIR, is_excluded

_CHUNK = 1024 * 1024


class NasProvider:
    """Files below ``root``. Deleted files move to ``<root>/.homeward-papierkorb/<date>/``.

    Writes go to a temporary file next to the target and are hard-linked into place,
    so a crash never leaves a partial file and an existing file is never clobbered.
    """

    def __init__(
        self,
        root: Path,
        *,
        name: str = "nas",
        trash_root: Path | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.name = name
        self.root = root
        self.trash_root = trash_root or root / TRASH_DIR
        self._today = today

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
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
        try:
            with tmp.open("xb") as out:
                shutil.copyfileobj(data, out, _CHUNK)
                out.flush()
                os.fsync(out.fileno())
            os.utime(tmp, (mtime, mtime))
            os.link(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return self._meta(path, target)

    def delete(self, path: str) -> None:
        source = self._full(path)
        day_dir = self.trash_root / self._today().isoformat()
        target = day_dir / path
        number = 2
        while target.exists():
            target = day_dir / f"{path} ({number})"
            number += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def move(self, path: str, new_path: str) -> FileMeta:
        source, target = self._full(path), self._full(new_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)  # fails if target exists
        source.unlink()
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
