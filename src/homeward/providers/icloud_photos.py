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
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast

from homeward.core.photos import FAVORITES_ALBUM, Library, PhotoItem, PhotoSource
from homeward.core.progress import Progress
from homeward.providers import pyicloud_compat as compat

log = logging.getLogger(__name__)

PERSONAL_KEY = "root"
SHARED_PREFIX = "shared:"
USER_ALBUM_CLASS = "PhotoAlbum"  # smart albums and folders are subclasses with other names
SMART_ALBUM_CLASS = "SmartPhotoAlbum"
HIDDEN_ALBUM = "Hidden"
EDITED_SUFFIX = " (bearbeitet)"
TIMEOUT = 60
# CloudKit download URLs are signed and expire soon after listing (well within the hour
# a first backup's listing alone can take); Apple then answers 410. So every batch of
# downloads gets fresh URLs first, unless they were renewed a moment ago.
EXPIRED_STATUS = 410
UNAUTHORIZED_STATUS = (401, 403)
RENEW_AFTER = 5 * 60
LOOKUP_BATCH = 100  # photos per lookup request (two records each)
TRANSIENT_STATUS = (500, 502, 503, 504)
RETRY_DELAY = 10.0
# Apple sometimes re-indexes a library (seen on the Shared Library); pyicloud then refuses
# to open it until indexing has finished.
INDEXING_ERROR = "PyiCloudServiceNotActivatedException"
# Short waits within a run: the daemon tries a waiting job again soon anyway, and a
# run that sleeps holds up every other job.
INDEXING_WAITS = (60.0, 120.0)
PROGRESS_EVERY = 500
# Changed records a quick run can handle on its own. Anything else (album memberships,
# albums, unknown types) makes it read the whole library instead.
CHANGE_TYPES = frozenset({"CPLAsset", "CPLMaster"})

# Edited renders live on the asset record (the master record holds the originals).
EDITED_PREFIXES = (("edited", "resJPEGFull"), ("edited_video", "resVidFull"))

XMP_WORTH_KEEPING = (
    "title", "description", "keywords", "gps_latitude", "gps_longitude", "rating",
)  # fmt: skip
XmpBuilder = Callable[[Any], bytes | None]
# (library, asset ids, master ids) -> fresh assets of that library
Lookup = Callable[[Any, list[str], list[str]], Iterable[Any]]
# photos service -> (the libraries that opened, by key; the ones still indexing, by key)
ZoneOpener = Callable[[Any], tuple[dict[str, Any], dict[str, "Indexing"]]]


class PhotosNotReady(Exception):
    """iCloud kept refusing the photo library while it indexes; try again later."""


@dataclass(frozen=True, slots=True)
class Indexing:
    """What Apple reports for a library it is still indexing."""

    state: str | None = None  # e.g. RUNNING
    progress: int | None = None  # percent, when Apple says


@dataclass(frozen=True, slots=True)
class _Resource:
    filename: str
    url: str | None
    size: int | None
    checksum: str | None


@dataclass(frozen=True, slots=True)
class _Origin:
    """Where to look an asset up again for a new download URL."""

    library: Any  # the CloudKit zone the asset lives in
    asset_id: str
    master_id: str
    resource: str


@dataclass(frozen=True, slots=True)
class PhotoOptions:
    """What a photos job downloads besides the originals (see ``PhotosConfig``)."""

    albums: bool = True
    videos: bool = True
    live_videos: bool = True
    raw_alternative: bool = True
    edited: bool = True
    favorites: bool = True
    hidden: bool = False
    xmp: bool = True
    album_names: tuple[str, ...] = ()  # only these albums (and favorites); empty: all


class ICloudPhotosSource(PhotoSource):
    def __init__(
        self,
        service: Any,
        libraries: Iterable[Library] = tuple(Library),
        options: PhotoOptions | None = None,
        *,
        xmp_builder: XmpBuilder | None = None,
        lookup: Lookup | None = None,
        open_zones: ZoneOpener | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        name: str = "icloud-photos",
        progress: Progress | None = None,
    ) -> None:
        self.name = name
        self.progress = progress or Progress()
        self.service = service
        self.wanted = tuple(libraries)
        self.options = options or PhotoOptions()
        self._xmp_builder = xmp_builder or pyicloud_xmp
        self._lookup = lookup or pyicloud_lookup
        self._open_zones = open_zones or pyicloud_open_zones
        self._opened: dict[str, Any] | None = None  # libraries opened around an indexing zone
        self._clock = clock
        self._sleep = sleep
        # Downloads run on several threads. pyicloud's session rewrites its session files on
        # every request, so threads download with their own plain sessions and every call
        # that goes through pyicloud holds this lock.
        self._api_lock = threading.Lock()
        self._local = threading.local()
        self._urls: dict[str, str] = {}
        self._blobs: dict[str, bytes] = {}
        self._origins: dict[str, _Origin] = {}
        # shared album items -> their album, which is listed again for fresh links
        self._stream_albums: dict[str, Any] = {}
        self._albums_relisted: dict[str, float] = {}
        self._fetched_at: dict[str, float] = {}

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
        switches = (on for on in astuple(self.options) if isinstance(on, bool))
        text = "".join("1" if on else "0" for on in switches)
        if self.options.album_names:
            names = "\n".join(sorted(self.options.album_names)).encode()
            text += "-" + hashlib.sha256(names).hexdigest()[:12]
        return text

    def list_items(self) -> Iterator[PhotoItem]:
        self._urls.clear()
        self._blobs.clear()
        self._origins.clear()
        self._fetched_at.clear()
        personal = self._photo_libraries()[PERSONAL_KEY]
        # Albums live in the personal library but may hold Shared Library photos too.
        # (pyicloud 2.7 cannot read the Shared Library's own albums.)
        members = self._album_members(personal)
        unmatched = set(members)
        libraries = self._libraries()
        self.progress.phase("listing", total=_photo_count(libraries))
        for lib, key, library in libraries:
            count = 0
            for count, asset in enumerate(library.all.photos, 1):
                self.progress.advance()
                unmatched.discard(asset.id)
                yield from self._items(asset, lib, members.get(asset.id, ()), origin=library)
                if count % PROGRESS_EVERY == 0:
                    log.info("library %s: %d photos read", key, count)
            log.info("library %s: %d photos read, done", key, count)
        if unmatched:
            log.info("%d album photos were not found in the selected libraries", len(unmatched))
        if self.options.hidden and Library.PERSONAL in self.wanted:
            yield from self._hidden_items(personal, members)
        if Library.SHARED_ALBUMS in self.wanted:
            yield from self._shared_album_items()

    def list_changes(self, since: str) -> list[PhotoItem] | None:
        """The files of the photos added or changed since the cursor ``since``, from
        each library's CloudKit change feed; None when that is not enough to be sure."""
        reason = self._no_changes_reason(since)
        if reason is not None:
            log.info("cannot read only the changes: %s", reason)
            return None
        self._urls.clear()
        self._blobs.clear()
        self._origins.clear()
        self._fetched_at.clear()
        tokens = _cursor_parts(since)
        items: list[PhotoItem] = []
        self.progress.phase("listing")
        for lib, key, library in self._libraries():
            changed = self._changed_assets(key, library, tokens[key])
            if changed is None:
                return None
            for asset in changed:
                items.extend(self._changed_items(asset, lib, library))
                self.progress.advance()
        return items

    def _no_changes_reason(self, since: str) -> str | None:
        current = self.cursor()
        if current is None:
            return "iCloud gave no sync token"
        old, new = _cursor_parts(since), _cursor_parts(current)
        if old.get("options") != new.get("options"):
            return "the backup settings changed"
        if old.get("shared_albums") != new.get("shared_albums"):
            return "the shared albums changed"
        missing = [key for _lib, key, _library in self._libraries() if key not in old]
        if missing:
            return f"no earlier sync token for {', '.join(missing)}"
        return None

    def _changed_assets(self, key: str, library: Any, since: str) -> list[Any] | None:
        """Fresh assets of every photo the change feed of one library reports."""
        pairs: dict[str, str] = {}  # asset record -> master record
        masters: set[str] = set()
        types: dict[str, int] = defaultdict(int)
        removed = 0
        client = compat.cloudkit_client(library)
        if client is None:
            log.info("library %s: this pyicloud session has no CloudKit client", key)
            return None
        try:
            with self._api_lock:
                request = compat.zone_changes_request(library, since)
                for zone in client.iter_changes(zone_req=request):
                    for record in zone.records:
                        error = getattr(record, "serverErrorCode", None)
                        if error is not None:
                            log.info("library %s: the change feed reported %s", key, error)
                            return None
                        record_type = getattr(record, "recordType", None)
                        if record_type is None or getattr(record, "deleted", False):
                            removed += 1  # deletions: the next full reading marks them
                            continue
                        types[record_type] += 1
                        if record_type == "CPLAsset":
                            ref = _field(record, "masterRef")
                            master = getattr(ref, "recordName", None)
                            if master is None and isinstance(ref, dict):
                                master = ref.get("recordName")
                            if master is None:
                                log.info("library %s: a changed photo has no master", key)
                                return None
                            pairs[record.recordName] = master
                        elif record_type == "CPLMaster":
                            masters.add(record.recordName)
        except Exception as exc:  # e.g. an expired sync token
            log.info("library %s: could not read the changes: %s", key, exc)
            return None
        summary = ", ".join(f"{n} {t}" for t, n in sorted(types.items())) or "none"
        log.info("library %s changes: %s; %d removed", key, summary, removed)
        unknown = set(types) - CHANGE_TYPES
        if unknown:
            names = ", ".join(sorted(unknown))
            log.info("library %s: changes of %s need a full reading", key, names)
            return None
        if masters - set(pairs.values()):
            log.info("library %s: an original changed without its photo", key)
            return None
        assets: list[Any] = []
        ids = sorted(pairs)
        for start in range(0, len(ids), LOOKUP_BATCH):
            batch = ids[start : start + LOOKUP_BATCH]
            with self._api_lock:
                found = list(self._lookup(library, batch, [pairs[i] for i in batch]))
            assets.extend(found)
        return assets

    def _changed_items(self, asset: Any, lib: Library, library: Any) -> Iterator[PhotoItem]:
        record = getattr(asset, "asset_record", None)
        if _field(record, "isDeleted") == 1 or _field(record, "isExpunged") == 1:
            return  # in "Recently Deleted": a backup keeps what it has
        hidden = _field(record, "isHidden") == 1
        if hidden and not (self.options.hidden and lib is Library.PERSONAL):
            return
        yield from self._items(asset, lib, (), hidden=hidden, origin=library)

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
            if _status(exc) in TRANSIENT_STATUS:  # Apple's CDN hiccups now and then
                log.info(
                    "iCloud answered %s for %s, retrying in %d s",
                    _status(exc), item.filename, RETRY_DELAY,
                    extra={"event": "apple_unavailable", "until": time.time() + RETRY_DELAY},
                )  # fmt: skip
                self._sleep(RETRY_DELAY)
                return self._stream(self._get(url))
            fresh = self._fresh_url(item.key) if _expired(exc) else None
            if fresh is None:
                raise
            log.info(
                "download link of %s had expired, fetched a new one",
                item.filename,
                extra={"event": "link_expired"},
            )
            self._urls[item.key] = fresh
            self._fetched_at[item.key] = self._clock()
            response = self._get(fresh)
        return self._stream(response)

    @staticmethod
    def _stream(response: Any) -> BinaryIO:
        raw = response.raw
        raw.decode_content = True
        return cast(BinaryIO, raw)

    def _get(self, url: str) -> Any:
        response = self._download_session().get(url, stream=True, timeout=TIMEOUT)
        if getattr(response, "status_code", 200) in UNAUTHORIZED_STATUS:  # wants iCloud's session
            response.close()
            with self._api_lock:
                response = self.service.session.get(url, stream=True, timeout=TIMEOUT)
        response.raise_for_status()
        return response

    def _download_session(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            with self._api_lock:
                session = self._local.session = download_session(self.service.session)
        return session

    def prepare(self, items: Sequence[PhotoItem]) -> None:
        """Renew URLs that may have expired since listing, a batch lookup per library."""
        now = self._clock()
        due: dict[int, list[tuple[str, _Origin]]] = defaultdict(list)
        for item in items:
            origin = self._origins.get(item.key)
            if origin is not None and now - self._fetched_at.get(item.key, now) >= RENEW_AFTER:
                due[id(origin.library)].append((item.key, origin))
        renewed = 0
        for entries in due.values():
            by_asset: dict[str, list[tuple[str, _Origin]]] = defaultdict(list)
            for key, origin in entries:
                by_asset[origin.asset_id].append((key, origin))
            groups = list(by_asset.values())
            for start in range(0, len(groups), LOOKUP_BATCH):
                renewed += self._renew(groups[start : start + LOOKUP_BATCH], now)
        if due:
            total = sum(len(e) for e in due.values())
            log.info("renewed %d of %d expiring download links", renewed, total)
        stale_albums = {
            id(album): album
            for item in items
            if (album := self._stream_albums.get(item.key)) is not None
            and now - self._fetched_at.get(item.key, now) >= RENEW_AFTER
        }
        for album in stale_albums.values():
            self._relist_shared_album(album, "")

    def _renew(self, groups: list[list[tuple[str, _Origin]]], now: float) -> int:
        origins = [group[0][1] for group in groups]
        try:
            with self._api_lock:
                found = self._lookup(
                    origins[0].library,
                    [o.asset_id for o in origins],
                    [o.master_id for o in origins],
                )
            fresh = {asset.id: asset for asset in found}
        except Exception as exc:  # each download falls back to a single lookup on 410
            log.warning("could not renew download links: %s", exc)
            return 0
        renewed = 0
        for group in groups:
            for key, origin in group:
                asset = fresh.get(origin.asset_id)
                url = _url(asset, origin.resource) if asset is not None else None
                if url:
                    self._urls[key] = url
                    self._fetched_at[key] = now
                    renewed += 1
        return renewed

    def _fresh_url(self, key: str) -> str | None:
        """Look the asset up again by name for a new signed URL of the same resource.

        (pyicloud's ``album.get`` scans the whole library when its query misses.)
        """
        album = self._stream_albums.get(key)
        if album is not None:
            return self._relist_shared_album(album, key)
        origin = self._origins.get(key)
        if origin is None:
            return None
        try:
            with self._api_lock:
                found = list(self._lookup(origin.library, [origin.asset_id], [origin.master_id]))
        except Exception as exc:
            log.warning("could not refresh %s: %s", key, exc)
            return None
        return _url(found[0], origin.resource) if found else None

    # Listing

    def _libraries(self) -> list[tuple[Library, str, Any]]:
        found = self._photo_libraries()
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

    def _photo_libraries(self) -> dict[str, Any]:
        """pyicloud's libraries, waiting while iCloud is still indexing one this backup
        needs. A library it does not need (another library or zone) is left out."""
        if self._opened is not None:
            return self._opened
        for wait in (*INDEXING_WAITS, None):
            try:
                return cast(dict[str, Any], self.service.photos.libraries)
            except Exception as exc:
                if not _indexing(exc):
                    raise
                waiting = self._indexing_zones()
                if waiting is not None and not waiting:
                    return cast(dict[str, Any], self._opened)
                what = _describe_indexing(waiting)
                if wait is None:
                    raise PhotosNotReady(
                        f"iCloud is still indexing {what}; the next run will try again"
                    ) from exc
                log.info(
                    "iCloud is still indexing %s, waiting %d s and trying again",
                    what,
                    wait,
                    extra={"event": "icloud_indexing", "until": time.time() + wait},
                )
                self._sleep(wait)
        raise AssertionError("unreachable")

    def _indexing_zones(self) -> dict[str, Indexing] | None:
        """Which libraries this backup needs are still indexing (None: unknown). When
        none are, the ones that opened are kept for this run."""
        try:
            photos = self.service.photos
        except Exception as exc:  # the personal library refused before anything else
            if _indexing(exc):
                return {PERSONAL_KEY: Indexing()}
            raise
        try:
            opened, waiting = self._open_zones(photos)
        except Exception as exc:
            log.debug("could not check the libraries one by one: %s", exc)
            return None
        needed = {key: state for key, state in waiting.items() if self._needs(key)}
        if needed:
            return needed
        for key, state in sorted(waiting.items()):
            log.info(
                "iCloud is still indexing %s, which this backup does not include; "
                "going on without it",
                _describe_indexing({key: state}),
            )
        self._opened = opened
        return {}

    def _needs(self, key: str) -> bool:
        if key == PERSONAL_KEY:
            return True  # albums and the shared albums' lookups go through it
        return key.startswith(SHARED_PREFIX) and Library.SHARED in self.wanted

    def _shared_albums(self) -> list[Any]:
        try:
            return list(self.service.photos.shared_streams)
        except Exception as exc:  # the account has no shared albums service
            log.info("shared albums not available: %s", exc)
            return []

    def _album_members(self, library: Any) -> dict[str, tuple[str, ...]]:
        members: dict[str, set[str]] = defaultdict(set)
        if self.options.albums:
            albums = [a for a in library.albums if type(a).__name__ == USER_ALBUM_CLASS]
            if self.options.album_names:
                wanted = set(self.options.album_names)
                albums = [a for a in albums if a.fullname in wanted]
            if Library.PERSONAL not in self.wanted:
                # Albums belong to the Apple ID, not to a library: they can hold
                # Shared Library photos, which get their album folders this way.
                log.info(
                    "reading your %d albums for photos of the selected libraries "
                    "(turn off albums as folders to skip this)", len(albums),
                )  # fmt: skip
            self.progress.phase("albums", total=len(albums))
            for number, album in enumerate(albums, 1):
                name = album.fullname
                log.info("reading album %d of %d: %s", number, len(albums), name)
                for asset in album.photos:
                    members[asset.id].add(name)
                self.progress.advance(current=name)
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
                        origin=library,
                    )  # fmt: skip
                log.info("hidden photos: %d read", count)
                return

    def _shared_album_items(self) -> Iterator[PhotoItem]:
        for album in self._shared_albums():
            count = 0
            for count, item in enumerate(self._shared_album_photos(album), 1):  # noqa: B007
                yield item
            log.info("shared album %s: %d files read", album.fullname, count)

    def _shared_album_photos(self, album: Any) -> Iterator[PhotoItem]:
        for asset in album.photos:
            for item in self._items(
                asset, Library.SHARED_ALBUMS, (), collection=album.fullname, id_prefix=album.id
            ):
                self._stream_albums[item.key] = album
                yield item

    def _relist_shared_album(self, album: Any, key: str) -> str | None:
        """Shared album links expire too (Apple then answers 401); listing the album
        again renews the links of all its files, at most once per RENEW_AFTER."""
        now = self._clock()
        if now - self._albums_relisted.get(album.id, -RENEW_AFTER) >= RENEW_AFTER:
            self._albums_relisted[album.id] = now
            try:
                with self._api_lock:
                    for _item in self._shared_album_photos(album):
                        pass
            except Exception as exc:
                log.warning("could not list shared album %s again: %s", album.fullname, exc)
                return None
        return self._urls.get(key)

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
        if is_movie and not self.options.videos:
            return
        resources = _resources(asset)
        record = getattr(asset, "asset_record", None)
        if self.options.favorites and record is not None and _field(record, "isFavorite") == 1:
            albums = (*albums, FAVORITES_ALBUM)
        wanted = self._wanted(asset, record, resources, is_movie=is_movie)
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
            item = self._resource_item(asset, base, key, resources.get(key), origin)
            if item is None:
                continue
            if key == "original":
                original_name = item.filename
            yield item

        if self.options.xmp and record is not None and original_name is not None:
            sidecar = self._xmp_item(asset, base, original_name)
            if sidecar is not None:
                yield sidecar

    def _resource_item(
        self, asset: Any, base: PhotoItem, key: str, resource: _Resource | None, origin: Any
    ) -> PhotoItem | None:
        """One downloadable file of an asset; remembers where to download it from."""
        if resource is None or not resource.url:
            if key == "original":
                log.warning("asset %s has no original to download", asset.id)
            return None
        item = base.variant(
            key, resource.filename or asset.filename, resource.size, resource.checksum
        )
        self._urls[item.key] = resource.url
        self._fetched_at[item.key] = self._clock()
        if origin is not None:
            master_id = getattr(asset, "master_id", asset.id)
            self._origins[item.key] = _Origin(origin, asset.id, master_id, key)
        return item

    def _wanted(
        self, asset: Any, record: Any, resources: dict[str, _Resource], *, is_movie: bool
    ) -> list[str]:
        """The resources of an asset to download; adds edited renders to ``resources``."""
        wanted = ["original"]
        if asset.is_live_photo and self.options.videos and self.options.live_videos:
            wanted.append("original_video")
        if self.options.raw_alternative:
            wanted.append("alternative")
        if self.options.edited and record is not None:
            for key, render in _edited(asset, record).items():
                if key == "edited_video" and not (is_movie or self.options.live_videos):
                    continue  # the edited video part of a Live Photo
                resources[key] = render
                wanted.append(key)
        return wanted

    def _xmp_item(self, asset: Any, base: PhotoItem, original_name: str) -> PhotoItem | None:
        try:
            data = self._xmp_builder(asset)
        except Exception as exc:
            log.warning("no XMP sidecar for %s: %s", asset.id, exc)
            return None
        if not data:
            return None
        item = base.variant(
            "xmp",
            f"{original_name}.xmp",
            len(data),
            hashlib.sha256(data).hexdigest(),
            sidecar_for="original",
        )
        self._blobs[item.key] = data
        return item


def _cursor_parts(cursor: str) -> dict[str, str]:
    """``key=value|key=value`` as written by ``cursor()``."""
    return dict(part.split("=", 1) for part in cursor.split("|") if "=" in part)


def _photo_count(libraries: list[tuple[Library, str, Any]]) -> int | None:
    """How many photos the libraries hold (one count query each), None if unknown."""
    try:
        return sum(len(library.all) for _lib, _key, library in libraries)
    except Exception as exc:  # only the progress bar needs it
        log.debug("could not count the photos: %s", exc)
        return None


def _resources(asset: Any) -> dict[str, _Resource]:
    """Downloadable files of an asset: CloudKit ``resources`` or legacy ``versions``."""
    result: dict[str, _Resource] = {}
    resources = getattr(asset, "resources", None)
    if resources is not None:
        for key, res in resources.items():
            result[key] = _Resource(res.filename, res.url, _size(res.size), res.checksum)
        return result
    for key, version in (getattr(asset, "versions", None) or {}).items():
        result[key] = _Resource(
            version.get("filename") or asset.filename,
            version.get("url"),
            _size(version.get("size")),
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
            f"{stem}{EDITED_SUFFIX}{ext}", url, _size(size), _field(record, f"{prefix}Fingerprint")
        )
    return result


def _url(asset: Any, resource: str) -> str | None:
    """The current download URL of one resource of a freshly fetched asset."""
    resources = _resources(asset)
    record = getattr(asset, "asset_record", None)
    if record is not None:
        resources.update(_edited(asset, record))
    found = resources.get(resource)
    return found.url if found is not None and found.url else None


def _indexing(exc: BaseException) -> bool:
    """True if iCloud refused because it is indexing; pyicloud may wrap that refusal
    (opening the root library raises PyiCloudServiceUnavailable from it)."""
    seen: BaseException | None = exc
    while seen is not None:
        if type(seen).__name__ == INDEXING_ERROR:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _status(exc: Exception) -> object:
    """The HTTP status of a failed request, as pyicloud or requests report it."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return getattr(getattr(exc, "response", None), "status_code", None)


def _size(value: object) -> int | None:
    """A file size Apple reported, or None when it is unknown. Older shared albums
    report 0 for every file, which must not fail the download's size check."""
    try:
        size = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _expired(exc: Exception) -> bool:
    """An expired download URL: pyicloud reports Apple's 410 as a withdrawn endpoint;
    shared album links answer 401 or 403 once they expire."""
    return (
        _status(exc) in (*UNAUTHORIZED_STATUS, EXPIRED_STATUS)
        or type(exc).__name__ == "PyiCloudEndpointGoneException"
    )


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

    metadata = compat.xmp_metadata(asset.asset_record)
    # Date and orientation are in the file's EXIF already; skip sidecars that add nothing.
    if metadata is None or not any(getattr(metadata, name) for name in XMP_WORTH_KEEPING):
        return None
    element = compat.render_xmp(metadata)
    return cast(bytes, ElementTree.tostring(element, encoding="utf-8", xml_declaration=True))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def pyicloud_lookup(library: Any, asset_ids: list[str], master_ids: list[str]) -> list[Any]:
    """Fetch assets of a pyicloud CloudKit library by record name in one request."""
    client = compat.cloudkit_client(library)
    if client is None:
        raise RuntimeError("this pyicloud session has no CloudKit client")
    response = client.lookup(
        record_names=[*asset_ids, *master_ids], zone_id=compat.zone_id_request(library.zone_id)
    )
    records = [record for record in response.records if compat.is_record(record)]
    assets_by_master, masters = compat.master_asset_pairs(records)
    return [
        library.asset_type(
            library.service, master, assets_by_master[master.recordName], library=library
        )
        for master in masters
        if master.recordName in assets_by_master
    ]


def pyicloud_open_zones(photos: Any) -> tuple[dict[str, Any], dict[str, Indexing]]:
    """Open the libraries of pyicloud's photos service one by one, the way
    ``photos.libraries`` does, but keep going past one that is still indexing.

    pyicloud opens every zone of the private database and gives up on the first one
    Apple is still indexing, even one no backup reads.
    """
    client = photos.private_client
    opened: dict[str, Any] = {PERSONAL_KEY: compat.root_library(photos)}
    primary = compat.primary_zone_name()
    waiting: dict[str, Indexing] = {}
    for zone in client.zones_list().zones:
        name = zone.zoneID.zoneName
        if zone.deleted:
            continue
        if name == primary:
            opened[PERSONAL_KEY].current_sync_token = zone.syncToken
            continue
        shared = compat.is_shared_library_zone(name)
        key = f"{SHARED_PREFIX}{name}" if shared else name
        zone_id = zone.zoneID.model_dump(exclude_none=True)
        try:
            opened[key] = compat.open_library(photos, zone_id, client, shared=shared)
        except Exception as exc:
            if not _indexing(exc):
                raise
            waiting[key] = _indexing_state(client, zone_id)
    return opened, waiting


def _indexing_state(client: Any, zone_id: dict[str, Any]) -> Indexing:
    try:
        response = client.query(
            query=compat.indexing_state_query(),
            zone_id=compat.zone_id_request(zone_id),
            results_limit=1,
        )
        for record in response.records:
            if compat.is_record(record):
                state = record.fields.get_value("state")
                progress = record.fields.get_value("progress")
                return Indexing(
                    None if state is None else str(state),
                    int(progress) if isinstance(progress, int | float) else None,
                )
    except Exception as exc:
        log.debug("could not read the indexing state: %s", exc)
    return Indexing()


def _describe_indexing(waiting: dict[str, Indexing] | None) -> str:
    """E.g. "the Shared Library (73 %)"."""
    if not waiting:
        return "the photo library"
    parts = []
    for key, state in sorted(waiting.items()):
        if key == PERSONAL_KEY:
            name = "the personal library"
        elif key.startswith(SHARED_PREFIX):
            name = "the Shared Library"
        else:
            name = f"the library zone {key}"
        parts.append(name if state.progress is None else f"{name} ({state.progress} %)")
    return ", ".join(parts)


def download_session(icloud_session: Any) -> Any:
    """A plain requests session with the iCloud cookies and headers, for one download
    thread. Falls back to the given session when it is not a requests session."""
    cookies = getattr(icloud_session, "cookies", None)
    headers = getattr(icloud_session, "headers", None)
    if cookies is None or headers is None:
        return icloud_session
    requests = importlib.import_module("requests")
    session = requests.Session()
    session.headers.update(headers)
    session.cookies.update(cookies)
    return session
