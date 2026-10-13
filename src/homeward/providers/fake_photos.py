"""In-memory fake iCloud Photos source for tests."""

import io
import itertools
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import BinaryIO

from homeward.core.photos import Library, PhotoItem, PhotoSource

_tokens = itertools.count(1)


class FakePhotoSource(PhotoSource):
    """Photos keyed by ``PhotoItem.key``. ``add``/``remove``/``edit`` simulate changes in
    iCloud; each change moves the cursor like a real library's sync token."""

    def __init__(self, name: str = "fake-photos") -> None:
        self.name = name
        self.items: dict[str, PhotoItem] = {}
        self.data: dict[str, bytes] = {}
        self.fail_keys: set[str] = set()
        self.listed = 0
        self.downloads: list[str] = []
        self.prepared: list[list[str]] = []
        self._cursor = f"c{next(_tokens)}"

    def add(
        self,
        asset_id: str,
        filename: str,
        data: bytes = b"",
        *,
        created: datetime = datetime(2024, 5, 17, 12, 0, tzinfo=UTC),
        library: Library = Library.PERSONAL,
        albums: tuple[str, ...] = (),
        resource: str = "original",
        hidden: bool = False,
        collection: str | None = None,
        sidecar_for: str | None = None,
    ) -> PhotoItem:
        payload = data or f"{asset_id}/{resource}".encode()
        item = PhotoItem(
            asset_id, resource, library, filename, created, len(payload),
            f"ck-{hash(payload) & 0xFFFFFFFF:x}", albums, hidden, collection, sidecar_for,
        )  # fmt: skip
        self.items[item.key] = item
        self.data[item.key] = payload
        self._touch()
        return item

    def edit(self, key: str, data: bytes) -> None:
        item = self.items[key]
        self.items[key] = replace(item, size=len(data), checksum=f"ck-{hash(data) & 0xFFFFFFFF:x}")
        self.data[key] = data
        self._touch()

    def set_albums(self, key: str, albums: tuple[str, ...]) -> None:
        self.items[key] = replace(self.items[key], albums=albums)
        self._touch()

    def remove(self, key: str) -> None:
        del self.items[key]
        del self.data[key]
        self._touch()

    def _touch(self) -> None:
        self._cursor = f"c{next(_tokens)}"

    # PhotoSource interface

    def cursor(self) -> str | None:
        return self._cursor

    def list_items(self) -> list[PhotoItem]:
        self.listed += 1
        return list(self.items.values())

    def prepare(self, items: Sequence[PhotoItem]) -> None:
        self.prepared.append([item.key for item in items])

    def open_read(self, item: PhotoItem) -> BinaryIO:
        if item.key in self.fail_keys:
            raise OSError(f"simulated failure for {item.key}")
        self.downloads.append(item.key)
        return io.BytesIO(self.data[item.key])
