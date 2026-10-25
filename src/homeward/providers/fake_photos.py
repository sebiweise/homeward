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
        self.deleted: list[str] = []  # asset ids deleted through ``delete_photo``
        # change log: (cursor after the change, key or None for an album change)
        self.changes_reliable = False  # tests switch the change feed on
        self.changes_listed = 0
        self._log: list[tuple[str, str | None]] = []
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
        self._touch(item.key)
        return item

    def edit(self, key: str, data: bytes) -> None:
        item = self.items[key]
        self.items[key] = replace(item, size=len(data), checksum=f"ck-{hash(data) & 0xFFFFFFFF:x}")
        self.data[key] = data
        self._touch(key)

    def set_albums(self, key: str, albums: tuple[str, ...]) -> None:
        self.items[key] = replace(self.items[key], albums=albums)
        self._touch()

    def remove(self, key: str) -> None:
        del self.items[key]
        del self.data[key]
        self._touch(key)

    def _touch(self, key: str | None = None) -> None:
        self._cursor = f"c{next(_tokens)}"
        self._log.append((self._cursor, key))

    # PhotoSource interface

    def cursor(self) -> str | None:
        return self._cursor

    def list_items(self) -> list[PhotoItem]:
        self.listed += 1
        return list(self.items.values())

    def list_changes(self, since: str) -> list[PhotoItem] | None:
        """Like iCloud's change feed: photos added or edited since ``since``. Removed
        photos are left out; an album change makes the answer unreliable (None)."""
        self.changes_listed += 1
        tokens = [token for token, _ in self._log]
        if not self.changes_reliable or since not in tokens:
            return None
        after = self._log[tokens.index(since) + 1 :]
        if any(key is None for _, key in after):
            return None
        keys = {key for _, key in after if key in self.items}
        return [self.items[key] for key in sorted(keys)]

    def prepare(self, items: Sequence[PhotoItem]) -> None:
        self.prepared.append([item.key for item in items])

    def open_read(self, item: PhotoItem) -> BinaryIO:
        if item.key in self.fail_keys:
            raise OSError(f"simulated failure for {item.key}")
        self.downloads.append(item.key)
        return io.BytesIO(self.data[item.key])

    def delete_photo(self, item: PhotoItem) -> None:
        """Delete the whole photo (all its files), like iCloud's "Recently Deleted"."""
        if item.key in self.fail_keys:
            raise OSError(f"simulated failure for {item.key}")
        for key in [k for k, i in self.items.items() if i.asset_id == item.asset_id]:
            self.remove(key)
        self.deleted.append(item.asset_id)
