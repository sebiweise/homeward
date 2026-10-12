"""iCloud Photos provider (pyicloud adapter).

Reads the personal library (``root``) and the iCloud Shared Library (``shared:*`` zones)
of a logged-in ``PyiCloudService`` and streams originals. Written against pyicloud 2.7's
CloudKit Photos service; pyicloud objects are used duck-typed, so tests can pass fakes.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, BinaryIO, cast

from homeward.core.photos import Library, PhotoItem

log = logging.getLogger(__name__)

PERSONAL_KEY = "root"
SHARED_PREFIX = "shared:"
USER_ALBUM_CLASS = "PhotoAlbum"  # smart albums and folders are subclasses with other names
TIMEOUT = 60


class ICloudPhotosSource:
    def __init__(
        self,
        service: Any,
        libraries: Iterable[Library] = (Library.PERSONAL, Library.SHARED),
        *,
        albums: bool = True,
        videos: bool = True,
        live_videos: bool = True,
        name: str = "icloud-photos",
    ) -> None:
        self.name = name
        self.service = service
        self.wanted = tuple(libraries)
        self.albums = albums
        self.videos = videos
        self.live_videos = live_videos
        self._urls: dict[str, str] = {}

    def _libraries(self) -> list[tuple[Library, str, Any]]:
        found = self.service.photos.libraries
        result: list[tuple[Library, str, Any]] = []
        if Library.PERSONAL in self.wanted:
            result.append((Library.PERSONAL, PERSONAL_KEY, found[PERSONAL_KEY]))
        if Library.SHARED in self.wanted:
            result.extend(
                (Library.SHARED, key, lib)
                for key, lib in sorted(found.items())
                if key.startswith(SHARED_PREFIX)
            )
        return result

    def cursor(self) -> str | None:
        parts = []
        for _lib, key, library in self._libraries():
            try:
                parts.append(f"{key}={library.sync_cursor()}")
            except Exception as exc:  # no token: list everything
                log.info("no sync token for %s: %s", key, exc)
                return None
        return "|".join(parts)

    def list_items(self) -> Iterator[PhotoItem]:
        self._urls.clear()
        for lib, key, library in self._libraries():
            # Shared Library albums are not readable through pyicloud 2.7.
            members = self._album_members(library) if lib is Library.PERSONAL else {}
            for asset in library.all.photos:
                yield from self._items(asset, lib, tuple(sorted(members.get(asset.id, ()))))
            log.info("listed library %s", key)

    def open_read(self, item: PhotoItem) -> BinaryIO:
        url = self._urls.get(item.key)
        if url is None:
            raise KeyError(f"{item.key} was not listed in this run")
        response = self.service.session.get(url, stream=True, timeout=TIMEOUT)
        response.raise_for_status()
        raw = response.raw
        raw.decode_content = True
        return cast(BinaryIO, raw)

    def _album_members(self, library: Any) -> dict[str, set[str]]:
        members: dict[str, set[str]] = defaultdict(set)
        if not self.albums:
            return members
        for album in library.albums:
            if type(album).__name__ != USER_ALBUM_CLASS:
                continue
            name = album.fullname
            for asset in album.photos:
                members[asset.id].add(name)
        return members

    def _items(self, asset: Any, lib: Library, albums: tuple[str, ...]) -> Iterator[PhotoItem]:
        if asset.item_type == "movie" and not self.videos:
            return
        resources = asset.resources
        created = _aware(asset.asset_date)
        wanted = ["original"]
        if asset.is_live_photo and self.videos and self.live_videos:
            wanted.append("original_video")
        for key in wanted:
            resource = resources.get(key)
            if resource is None or not resource.url:
                log.warning("asset %s has no %s to download", asset.id, key)
                continue
            item = PhotoItem(
                asset_id=asset.id,
                resource=key,
                library=lib,
                filename=resource.filename or asset.filename,
                created=created,
                size=resource.size,
                checksum=resource.checksum,
                albums=albums,
            )
            self._urls[item.key] = resource.url
            yield item


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
