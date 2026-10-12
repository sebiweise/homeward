"""In-memory fake provider for tests."""

import hashlib
import io
import itertools
from dataclasses import dataclass
from typing import BinaryIO

from homeward.core.models import FileMeta

_versions = itertools.count(1)


@dataclass(slots=True)
class _Entry:
    data: bytes
    mtime: float
    version: str


class FakeProvider:
    """A provider backed by a dict. Deleted files go to ``trash`` so tests can check them.

    ``put``/``remove`` simulate a user changing files outside Homeward; the ``Provider``
    methods are what the executor calls.
    """

    def __init__(
        self, name: str = "fake", *, known_hashes: bool = True, case_insensitive: bool = False
    ) -> None:
        self.name = name
        self.known_hashes = known_hashes
        self.case_insensitive = case_insensitive
        self.files: dict[str, _Entry] = {}
        self.trash: list[tuple[str, bytes]] = []
        self.fail_paths: set[str] = set()

    # Test helpers

    def put(self, path: str, data: bytes | str, mtime: float = 1_700_000_000.0) -> FileMeta:
        raw = data.encode() if isinstance(data, str) else data
        self.files[path] = _Entry(raw, mtime, f"{self.name}-{next(_versions)}")
        return self._meta(path)

    def remove(self, path: str) -> None:
        del self.files[path]

    def content(self, path: str) -> bytes:
        return self.files[path].data

    def trashed(self, path: str) -> list[bytes]:
        return [data for trashed_path, data in self.trash if trashed_path == path]

    # Provider interface

    def list_files(self) -> dict[str, FileMeta]:
        return {path: self._meta(path) for path in self.files}

    def open_read(self, path: str) -> BinaryIO:
        self._check(path)
        return io.BytesIO(self.files[path].data)

    def write(self, path: str, data: BinaryIO, mtime: float) -> FileMeta:
        self._check(path)
        self._check_free(path)
        return self.put(path, data.read(), mtime)

    def delete(self, path: str) -> None:
        self._check(path)
        self.trash.append((path, self.files.pop(path).data))

    def move(self, path: str, new_path: str) -> FileMeta:
        self._check(path)
        self._check_free(new_path)
        self.files[new_path] = self.files.pop(path)
        return self._meta(new_path)

    def sha256(self, path: str) -> str:
        return hashlib.sha256(self.files[path].data).hexdigest()

    def _check(self, path: str) -> None:
        if path in self.fail_paths:
            raise OSError(f"simulated failure for {path}")

    def _check_free(self, path: str) -> None:
        if path in self.files or (
            self.case_insensitive
            and any(other.casefold() == path.casefold() for other in self.files)
        ):
            raise FileExistsError(path)

    def _meta(self, path: str) -> FileMeta:
        entry = self.files[path]
        digest = hashlib.sha256(entry.data).hexdigest() if self.known_hashes else None
        return FileMeta(path, len(entry.data), entry.mtime, entry.version, digest)
