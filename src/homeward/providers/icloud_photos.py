"""iCloud Photos provider (pyicloud adapter).

Reads the personal library (``root``), the iCloud Shared Library (``shared:*`` zones) and
the older shared albums of a logged-in ``PyiCloudService`` and streams the files. Written
against pyicloud 2.7's CloudKit Photos service; pyicloud objects are used duck-typed, so
tests can pass fakes and pyicloud is only imported for XMP sidecars.
"""

import hashlib
import importlib
import io
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast

from homeward.core.photos import FAVORITES_ALBUM, Library, PhotoItem

log = logging.getLogger(__name__)

PERSONAL_KEY = "root"
SHARED_PREFIX = "shared:"
USER_ALBUM_CLASS = "PhotoAlbum"  # smart albums and folders are subclasses with other names
SMART_ALBUM_CLASS = "SmartPhotoAlbum"
HIDDEN_ALBUM = "Hidden"
EDITED_SUFFIX = " (bearbeitet)"
TIMEOUT = 60
# CloudKit download URLs are signed and expire after some hours; Apple then answers 410.
EXPIRED_STATUS = 410
PROGRESS_EVERY = 500

# Edited renders live on the asset record (the master record holds the originals).
EDITED_PREFIXES = (("edited", "resJPEGFull"), ("edited_video", "resVidFull"))

XMP_WORTH_KEEPING = (
    "title", "description", "keywords", "gps_latitude", "gps_longitude", "rating",
)  # fmt: skip
XmpBuilder = Callable[[Any], bytes | None]


@dataclass(frozen=True, slots=True)
class _Resource:
    filename: str
    url: str | None
    size: int | None
    checksum: str | None


class ICloudPhotosSource:
    def __init__(
        self,
        service: Any,
        libraries: Iterable[Library] = tuple(Library),
        *,
        albums: bool = True,
        videos: bool = True,
        live_videos: bool = True,
        raw_alternative: bool = True,
        edited: bool = True,
        favorites: bool = True,
        hidden: bool = False,
        xmp: bool = True,
        xmp_builder: XmpBuilder | None = None,
        name: str = "icloud-photos",
    ) -> None:
        self.name = name
        self.service = service
        self.wanted = tuple(libraries)
        self.albums = albums
        self.videos = videos
        self.live_videos = live_videos
        self.raw_alternative = raw_alternative
        self.edited = edited
        self.favorites = favorites
        self.hidden = hidden
        self.xmp = xmp
        self._xmp_builder = xmp_builder or pyicloud_xmp
        self._urls: dict[str, str] = {}
        self._blobs: dict[str, bytes] = {}
        # Where to look an asset up again when its URL has expired: (container, id, resource).
        self._origins: dict[str, tuple[Any, str, str]] = {}

    # PhotoSource interface

    def cursor(self) -> str | None:
        parts = []
        for _lib, key, library in self._libraries():
            try:
                parts.append(f"{key}={library.sync_cursor()}")
            except Exception as exc:  # no token: list everything
                log.info("no sync token for %s: %s", key, exc)
                return None
        if Library.SHARED_ALBUMS in self.wanted:
            tags = [getattr(album, "_album_ctag", None) for album in self._shared_albums()]
            if any(tag is None for tag in tags):
                return None
            if tags:
                parts.append("shared_albums=" + ",".join(cast(list[str], tags)))
        # Turning a feature on must list again even when nothing changed in iCloud.
        parts.append(f"options={self._options()}")
        return "|".join(parts)

    def _options(self) -> str:
        switches = (
            self.albums, self.videos, self.live_videos, self.raw_alternative, self.edited,
            self.favorites, self.hidden, self.xmp,
        )  # fmt: skip
        return "".join("1" if on else "0" for on in switches)

    def list_items(self) -> Iterator[PhotoItem]:
        self._urls.clear()
        self._blobs.clear()
        self._origins.clear()
        personal = self.service.photos.libraries[PERSONAL_KEY]
        # Albums live in the personal library but may hold Shared Library photos too.
        # (pyicloud 2.7 cannot read the Shared Library's own albums.)
        members = self._album_members(personal)
        unmatched = set(members)
        for lib, key, library in self._libraries():
            count = 0
            for count, asset in enumerate(library.all.photos, 1):
                unmatched.discard(asset.id)
                yield from self._items(asset, lib, members.get(asset.id, ()), origin=library.all)
                if count % PROGRESS_EVERY == 0:
                    log.info("library %s: %d photos read", key, count)
            log.info("library %s: %d photos read, done", key, count)
        if unmatched:
            log.info("%d album photos were not found in the selected libraries", len(unmatched))
        if self.hidden and Library.PERSONAL in self.wanted:
            yield from self._hidden_items(personal, members)
        if Library.SHARED_ALBUMS in self.wanted:
            yield from self._shared_album_items()

    def open_read(self, item: PhotoItem) -> BinaryIO:
        blob = self._blobs.get(item.key)
        if blob is not None:
            return io.BytesIO(blob)
        url = self._urls.get(item.key)
        if url is None:
            raise KeyError(f"{item.key} was not listed in this run")
        try:
            response = self._get(url)
        except Exception as exc:
            fresh = self._fresh_url(item.key) if _expired(exc) else None
            if fresh is None:
                raise
            log.debug("download URL of %s expired, fetched a new one", item.key)
            self._urls[item.key] = fresh
            response = self._get(fresh)
        raw = response.raw
        raw.decode_content = True
        return cast(BinaryIO, raw)

    def _get(self, url: str) -> Any:
        response = self.service.session.get(url, stream=True, timeout=TIMEOUT)
        response.raise_for_status()
        return response

    def _fresh_url(self, key: str) -> str | None:
        """Look the asset up again for a new signed URL of the same resource."""
        origin = self._origins.get(key)
        if origin is None:
            return None
        container, asset_id, resource = origin
        try:
            asset = container.get(asset_id)
        except Exception as exc:
            log.warning("could not refresh %s: %s", key, exc)
            return None
        if asset is None:
            return None
        resources = _resources(asset)
        record = getattr(asset, "asset_record", None)
        if record is not None:
            resources.update(_edited(asset, record))
        found = resources.get(resource)
        return found.url if found is not None and found.url else None

    # Listing

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

    def _shared_albums(self) -> list[Any]:
        try:
            return list(self.service.photos.shared_streams)
        except Exception as exc:  # the account has no shared albums service
            log.info("shared albums not available: %s", exc)
            return []

    def _album_members(self, library: Any) -> dict[str, tuple[str, ...]]:
        members: dict[str, set[str]] = defaultdict(set)
        if self.albums:
            albums = [a for a in library.albums if type(a).__name__ == USER_ALBUM_CLASS]
            for number, album in enumerate(albums, 1):
                name = album.fullname
                log.info("reading album %d of %d: %s", number, len(albums), name)
                for asset in album.photos:
                    members[asset.id].add(name)
        return {asset_id: tuple(sorted(names)) for asset_id, names in members.items()}

    def _hidden_items(
        self, library: Any, members: dict[str, tuple[str, ...]]
    ) -> Iterator[PhotoItem]:
        for album in library.albums:
            if type(album).__name__ == SMART_ALBUM_CLASS and album.fullname == HIDDEN_ALBUM:
                count = 0
                for count, asset in enumerate(album.photos, 1):  # noqa: B007
                    yield from self._items(
                        asset, Library.PERSONAL, members.get(asset.id, ()), hidden=True,
                        origin=album,
                    )  # fmt: skip
                log.info("hidden photos: %d read", count)
                return

    def _shared_album_items(self) -> Iterator[PhotoItem]:
        for album in self._shared_albums():
            count = 0
            for count, asset in enumerate(album.photos, 1):  # noqa: B007
                yield from self._items(
                    asset, Library.SHARED_ALBUMS, (), collection=album.fullname, id_prefix=album.id
                )
            log.info("shared album %s: %d photos read", album.fullname, count)

    def _items(
        self,
        asset: Any,
        lib: Library,
        albums: tuple[str, ...],
        *,
        hidden: bool = False,
        collection: str | None = None,
        id_prefix: str | None = None,
        origin: Any = None,
    ) -> Iterator[PhotoItem]:
        is_movie = asset.item_type == "movie"
        if is_movie and not self.videos:
            return
        resources = _resources(asset)
        record = getattr(asset, "asset_record", None)
        if self.favorites and record is not None and _field(record, "isFavorite") == 1:
            albums = (*albums, FAVORITES_ALBUM)
        wanted = ["original"]
        if asset.is_live_photo and self.videos and self.live_videos:
            wanted.append("original_video")
        if self.raw_alternative:
            wanted.append("alternative")
        if self.edited and record is not None:
            for key, render in _edited(asset, record).items():
                if key == "edited_video" and not (is_movie or self.live_videos):
                    continue  # the edited video part of a Live Photo
                resources[key] = render
                wanted.append(key)

        base = PhotoItem(
            asset_id=f"{id_prefix}:{asset.id}" if id_prefix else asset.id,
            resource="original",
            library=lib,
            filename=asset.filename,
            created=_aware(asset.asset_date),
            albums=albums,
            hidden=hidden,
            collection=collection,
        )
        original_name: str | None = None
        for key in wanted:
            resource = resources.get(key)
            if resource is None or not resource.url:
                if key == "original":
                    log.warning("asset %s has no original to download", asset.id)
                continue
            item = replace(
                base,
                resource=key,
                filename=resource.filename or asset.filename,
                size=resource.size,
                checksum=resource.checksum,
            )
            if key == "original":
                original_name = item.filename
            self._urls[item.key] = resource.url
            if origin is not None:
                self._origins[item.key] = (origin, asset.id, key)
            yield item

        if self.xmp and record is not None and original_name is not None:
            try:
                data = self._xmp_builder(asset)
            except Exception as exc:
                log.warning("no XMP sidecar for %s: %s", asset.id, exc)
                data = None
            if data:
                item = replace(
                    base,
                    resource="xmp",
                    filename=f"{original_name}.xmp",
                    size=len(data),
                    checksum=hashlib.sha256(data).hexdigest(),
                    albums=(),
                    sidecar_for="original",
                )
                self._blobs[item.key] = data
                yield item


def _resources(asset: Any) -> dict[str, _Resource]:
    """Downloadable files of an asset: CloudKit ``resources`` or legacy ``versions``."""
    result: dict[str, _Resource] = {}
    resources = getattr(asset, "resources", None)
    if resources is not None:
        for key, res in resources.items():
            result[key] = _Resource(res.filename, res.url, res.size, res.checksum)
        return result
    for key, version in (getattr(asset, "versions", None) or {}).items():
        result[key] = _Resource(
            version.get("filename") or asset.filename,
            version.get("url"),
            version.get("size"),
            version.get("checksum"),
        )
    return result


def _edited(asset: Any, record: Any) -> dict[str, _Resource]:
    """Full-size renders of edits made in Photos, named ``Name (bearbeitet).ext``."""
    result: dict[str, _Resource] = {}
    stem = PurePosixPath(asset.filename).stem
    extensions: dict[str, str] = getattr(asset, "FILE_TYPE_EXTENSIONS", {})
    for key, prefix in EDITED_PREFIXES:
        token = _field(record, f"{prefix}Res")
        if token is None:
            continue
        if isinstance(token, dict):
            url, size = token.get("downloadURL"), token.get("size")
        else:
            url, size = getattr(token, "downloadURL", None), getattr(token, "size", None)
        file_type = _field(record, f"{prefix}FileType")
        default = ".MOV" if key == "edited_video" else ".JPG"
        ext = extensions.get(file_type, default) if isinstance(file_type, str) else default
        result[key] = _Resource(
            f"{stem}{EDITED_SUFFIX}{ext}", url, size, _field(record, f"{prefix}Fingerprint")
        )
    return result


def _expired(exc: Exception) -> bool:
    """An expired download URL: pyicloud reports Apple's 410 as a withdrawn endpoint."""
    for value in (
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if value == EXPIRED_STATUS:
            return True
    return type(exc).__name__ == "PyiCloudEndpointGoneException"


def _field(record: Any, name: str) -> Any:
    """A field of a CloudKit record (typed pyicloud record or raw dict)."""
    fields = getattr(record, "fields", None)
    if fields is not None and hasattr(fields, "get_value"):
        value = fields.get_value(name)
    elif isinstance(record, dict):
        value = record.get("fields", {}).get(name)
    else:
        return None
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def pyicloud_xmp(asset: Any) -> bytes | None:
    """XMP sidecar (caption, keywords, location, favorite/hidden rating) built by
    pyicloud's own exporter, or None if the photo has none of these."""
    from xml.etree import ElementTree

    materialize = importlib.import_module("pyicloud.services.photos_cloudkit.materialize")
    metadata = materialize.build_xmp_metadata(asset.asset_record)
    # Date and orientation are in the file's EXIF already; skip sidecars that add nothing.
    if metadata is None or not any(getattr(metadata, name) for name in XMP_WORTH_KEEPING):
        return None
    element = materialize._render_xmp_xml(metadata)
    return cast(bytes, ElementTree.tostring(element, encoding="utf-8", xml_declaration=True))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
