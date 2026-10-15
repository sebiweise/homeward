"""iCloud Photos backup (iCloud -> NAS only): plan and apply one run of a photos job.

Every original lands once below ``<target>/<scheme>/`` (Year/Month by default); the
shared library and the shared albums go below their own folders unless
``PhotoLayout.source_folders`` is off (for a job that backs up one of them alone).
Album folders hold hard links to those files, so a photo in several albums takes its
disk space once.

A backup never deletes: photos removed in iCloud stay on the NAS, a changed original
moves the old file to the trash first, and only album links (never the last copy of
a photo) are removed when a photo leaves an album.
"""

import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from homeward.core.files import link_noreplace, rename_noreplace, temp_name
from homeward.core.photo_state import PhotoRecord, PhotoStateDB
from homeward.core.progress import Progress
from homeward.core.rules import TRASH_DIR

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f/\\:]+")
SCHEME_FIELDS = ("year", "month", "day")


class Library(StrEnum):
    PERSONAL = "personal"
    SHARED = "shared"  # the iCloud Shared Library
    SHARED_ALBUMS = "shared_albums"  # the older shared albums (photo streams)


# Pseudo album name for favorites; the NUL byte keeps it apart from real album names.
FAVORITES_ALBUM = "\x00favorites"


@dataclass(frozen=True, slots=True)
class PhotoItem:
    """One downloadable file of an iCloud photo (a Live Photo has two: image and video).

    ``checksum`` is iCloud's own change token for the original; ``None`` if unknown.
    ``created`` must be timezone aware.
    """

    asset_id: str
    resource: str
    library: Library
    filename: str
    created: datetime
    size: int | None = None
    checksum: str | None = None
    albums: tuple[str, ...] = ()
    hidden: bool = False
    collection: str | None = None  # the shared album of a ``SHARED_ALBUMS`` item
    sidecar_for: str | None = None  # resource whose NAS file this sidecar sits next to

    @property
    def key(self) -> str:
        return f"{self.library}/{self.asset_id}/{self.resource}"

    def variant(
        self,
        resource: str,
        filename: str,
        size: int | None,
        checksum: str | None,
        *,
        sidecar_for: str | None = None,
    ) -> "PhotoItem":
        """Another file of the same photo; a sidecar is never linked into albums."""
        return PhotoItem(
            asset_id=self.asset_id,
            resource=resource,
            library=self.library,
            filename=filename,
            created=self.created,
            size=size,
            checksum=checksum,
            albums=() if sidecar_for else self.albums,
            hidden=self.hidden,
            collection=self.collection,
            sidecar_for=sidecar_for,
        )


class PhotoSource(Protocol):
    """The iCloud side of a photos job, implemented in ``homeward.providers``."""

    name: str

    def cursor(self) -> str | None:
        """An opaque token that changes whenever anything in the libraries changes."""
        ...

    def list_items(self) -> Iterable[PhotoItem]:
        """Every file of every photo in the selected libraries."""
        ...

    def list_changes(self, since: str) -> Iterable[PhotoItem] | None:
        """The files of every photo added or changed since the cursor ``since``, or None
        when the source cannot tell for sure (then the whole library is read).

        Photos removed in iCloud and changed album memberships need not be reported:
        the regular full listing catches up on those.
        """
        ...

    def prepare(self, items: Sequence[PhotoItem]) -> None:
        """Called before the next batch of downloads; a source whose download URLs
        expire renews them here."""
        ...

    def open_read(self, item: PhotoItem) -> BinaryIO:
        """Stream the original. The caller closes it."""
        ...


@dataclass(frozen=True, slots=True)
class PhotoLayout:
    scheme: str = "{year}/{month}"
    shared_folder: str = "Geteilte Mediathek"
    albums_folder: str = "Alben"
    favorites_folder: str = "Favoriten"
    hidden_folder: str = "Ausgeblendet"
    shared_albums_folder: str = "Geteilte Alben"
    # Off: shared library and shared albums skip their own folder (one-source jobs).
    source_folders: bool = True

    def folder(self, item: PhotoItem) -> PurePosixPath:
        if item.library is Library.SHARED_ALBUMS:
            album = PurePosixPath(safe_component(item.collection or "", "_"))
            if not self.source_folders:
                return album
            return PurePosixPath(safe_component(self.shared_albums_folder)) / album
        when = item.created.astimezone()
        parts = self.scheme.format(
            year=f"{when.year:04d}", month=f"{when.month:02d}", day=f"{when.day:02d}"
        )
        folder = safe_relative(parts)
        if item.library is Library.SHARED and self.source_folders:
            folder = PurePosixPath(safe_component(self.shared_folder)) / folder
        if item.hidden:
            folder = PurePosixPath(safe_component(self.hidden_folder)) / folder
        return folder

    def album_folder(self, album: str) -> PurePosixPath:
        if album == FAVORITES_ALBUM:
            return PurePosixPath(safe_component(self.favorites_folder))
        return PurePosixPath(safe_component(self.albums_folder)) / safe_relative(album)


def check_scheme(scheme: str) -> None:
    """Raise ValueError unless ``scheme`` only uses ``{year}``, ``{month}`` and ``{day}``."""
    try:
        rendered = scheme.format(**dict.fromkeys(SCHEME_FIELDS, "0"))
    except (KeyError, IndexError, ValueError) as exc:
        fields = ", ".join(f"{{{name}}}" for name in SCHEME_FIELDS)
        raise ValueError(f"folder_scheme may only use {fields}: {exc}") from None
    if PurePosixPath(rendered).is_absolute() or ".." in PurePosixPath(rendered).parts:
        raise ValueError("folder_scheme must be a relative path")


def safe_component(value: str, fallback: str = "_") -> str:
    cleaned = _UNSAFE.sub("-", value).strip()
    return fallback if cleaned in {"", ".", ".."} else cleaned


def safe_relative(value: str) -> PurePosixPath:
    parts = [safe_component(p) for p in value.replace("\\", "/").split("/") if p not in {"", "."}]
    return PurePosixPath(*[p for p in parts if p != ".."]) if parts else PurePosixPath()


def numbered(path: PurePosixPath, number: int) -> PurePosixPath:
    return path.with_name(f"{path.stem} ({number}){path.suffix}")


class PhotoActionKind(StrEnum):
    DOWNLOAD = "download"  # new photo
    RESTORE = "restore"  # known photo whose NAS file is missing
    REPLACE = "replace"  # the original changed in iCloud; old file goes to the trash
    LINK = "link"  # add a photo to an album folder
    UNLINK = "unlink"  # the photo left the album; its Year/Month file stays
    GONE = "gone"  # deleted in iCloud; the NAS copy stays
    RETURNED = "returned"  # back in iCloud after it was gone (e.g. restored there)


_FILE_KINDS = frozenset(
    {PhotoActionKind.DOWNLOAD, PhotoActionKind.RESTORE, PhotoActionKind.REPLACE}
)
PROGRESS_EVERY = 50
PREPARE_BATCH = 100
FULL_LISTING_HOURS = 24  # read the whole library at least this often
DEFAULT_WORKERS = 4
MAX_WORKERS = 8


@dataclass(frozen=True, slots=True)
class PhotoAction:
    kind: PhotoActionKind
    path: str
    item: PhotoItem | None = None
    key: str = ""
    target: str | None = None  # LINK/UNLINK: the Year/Month file the link points to
    album: str = ""  # LINK/UNLINK


@dataclass(slots=True)
class PhotoPlan:
    job: str
    cursor: str | None
    unchanged: bool = False  # cursor unchanged and every file present: nothing listed
    full: bool = True  # the whole library was read (False: only the changes)
    started_at: float = 0.0  # when the plan asked iCloud
    actions: list[PhotoAction] = field(default_factory=list)
    up_to_date: int = 0

    def counts(self) -> dict[PhotoActionKind, int]:
        result: dict[PhotoActionKind, int] = {}
        for action in self.actions:
            result[action.kind] = result.get(action.kind, 0) + 1
        return result


@dataclass(frozen=True, slots=True)
class PhotoFailure:
    action: PhotoAction
    error: str


@dataclass(slots=True)
class PhotoRunResult:
    done: list[PhotoAction] = field(default_factory=list)
    failed: list[PhotoFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


class _Paths:
    """Hands out NAS paths that no other photo or link owns and that are free on disk.

    Names are compared ignoring case: Synology Drive (and SMB clients on macOS or
    Windows) see ``IMG_1.JPG`` and ``img_1.jpg`` as the same file and rename one of them.
    """

    def __init__(self, root: Path, owned: Iterable[str]) -> None:
        self.root = root
        self.taken = {p.casefold() for p in owned}
        self._listings: dict[PurePosixPath, set[str]] = {}

    def claim(self, wanted: PurePosixPath) -> str:
        candidate, number = wanted, 2
        while str(candidate).casefold() in self.taken or self._on_disk(candidate):
            candidate = numbered(wanted, number)
            number += 1
        self.taken.add(str(candidate).casefold())
        return str(candidate)

    def _on_disk(self, path: PurePosixPath) -> bool:
        return path.name.casefold() in _listing(self.root, path.parent, self._listings)


class _Folders:
    """Gives every album its own folder; folders differing only in case count as one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.used: dict[str, PurePosixPath] = {}  # casefolded folder or parent -> folder
        self.leaves: set[str] = set()  # casefolded album folders
        self._listings: dict[PurePosixPath, set[str]] = {}

    def keep(self, folder: PurePosixPath) -> PurePosixPath:
        self._register(folder)
        return folder

    def claim(self, wanted: PurePosixPath) -> PurePosixPath:
        parent = self._canonical(wanted.parent)
        name, number = wanted.name, 2
        while self._taken(parent / name):
            name = f"{wanted.name} ({number})"
            number += 1
        return self.keep(parent / name)

    def _canonical(self, folder: PurePosixPath) -> PurePosixPath:
        """``folder`` with each part spelled like a folder already in use."""
        result = PurePosixPath()
        for part in folder.parts:
            result = self.used.get(str(result / part).casefold(), result / part)
        return result

    def _taken(self, folder: PurePosixPath) -> bool:
        key = str(folder).casefold()
        if key in self.leaves or (key in self.used and self.used[key] != folder):
            return True
        names = _listing(self.root, folder.parent, self._listings)
        return folder.name.casefold() in names and not (self.root / folder).is_dir()

    def _register(self, folder: PurePosixPath) -> None:
        self.leaves.add(str(folder).casefold())
        for prefix in [folder, *folder.parents]:
            if prefix.parts:
                self.used.setdefault(str(prefix).casefold(), prefix)


def _listing(root: Path, folder: PurePosixPath, cache: dict[PurePosixPath, set[str]]) -> set[str]:
    """Casefolded names in ``root / folder``, read once per plan."""
    if folder not in cache:
        try:
            cache[folder] = {entry.casefold() for entry in os.listdir(root / folder)}
        except OSError:
            cache[folder] = set()
    return cache[folder]


def _all_present(root: Path, records: Iterable[PhotoRecord], links: Iterable[str]) -> bool:
    return all((root / r.path).exists() for r in records if r.gone_at is None) and all(
        (root / path).exists() for path in links
    )


def build_photo_plan(
    job: str,
    source: PhotoSource,
    root: Path,
    state: PhotoStateDB,
    layout: PhotoLayout | None = None,
    *,
    albums: bool = True,
    progress: Progress | None = None,
    now: Callable[[], float] = time.time,
) -> PhotoPlan:
    """Compare iCloud with the state of the last run and decide what to fetch or link.

    When only some photos changed since the last run, the source is asked for just
    those (``list_changes``) instead of the whole library. The whole library is still
    read when the source cannot tell for sure, when a file is missing on the NAS, and
    at least every ``FULL_LISTING_HOURS``; that run also catches up on photos removed
    in iCloud and on changed album memberships.
    """
    layout = layout or PhotoLayout()
    progress = progress or Progress()
    progress.phase("checking")
    records = state.records(job)
    links = state.links(job)
    cursor = source.cursor()
    previous = state.cursor(job)
    plan = PhotoPlan(job=job, cursor=cursor, started_at=now())
    present = _all_present(root, records.values(), links)
    if cursor is not None and cursor == previous and present:
        plan.unchanged = True
        plan.up_to_date = sum(1 for r in records.values() if r.gone_at is None)
        return plan

    paths = _Paths(root, [r.path for r in records.values()] + list(links))
    changes = _changes(job, source, state, cursor, previous, present, plan.started_at)
    if changes is not None:
        plan.full = False
        progress.phase("planning", total=len(changes))
        _, wanted_links = _plan_items(plan, changes, root, records, paths, layout, progress, albums)
        fetched = {a.key for a in plan.actions if a.kind in _FILE_KINDS}
        plan.up_to_date = sum(
            1 for k, r in records.items() if r.gone_at is None and k not in fetched
        )
        existing = {(key, album): path for path, (key, album) in links.items()}
        _plan_links(plan, root, layout, paths, existing, wanted_links)
        return plan

    progress.phase("listing")  # the source sets the total once it knows it
    items = sorted(source.list_items(), key=lambda i: (i.created, i.key))
    progress.phase("planning", total=len(items))
    seen, wanted_links = _plan_items(plan, items, root, records, paths, layout, progress, albums)
    for key, record in sorted(records.items()):
        if key not in seen and record.gone_at is None:
            plan.actions.append(PhotoAction(PhotoActionKind.GONE, record.path, key=key))
    existing = {(key, album): path for path, (key, album) in links.items()}
    _plan_links(plan, root, layout, paths, existing, wanted_links)
    _plan_unlinks(plan, records, existing, wanted_links, seen)
    return plan


def _changes(
    job: str,
    source: PhotoSource,
    state: PhotoStateDB,
    cursor: str | None,
    previous: str | None,
    present: bool,
    now: float,
) -> list[PhotoItem] | None:
    """The changed photos when reading only the changes is safe, else None."""
    full_at = state.full_listing_at(job)
    if cursor is None or previous is None or full_at is None:
        reason = "no earlier complete run to build on"
    elif not present:
        reason = "files are missing on the NAS"
    elif now - full_at >= FULL_LISTING_HOURS * 3600:
        reason = f"the last complete reading is more than {FULL_LISTING_HOURS} hours old"
    else:
        changed = source.list_changes(previous)
        if changed is not None:
            items = sorted(changed, key=lambda i: (i.created, i.key))
            log.info("reading only what changed in iCloud: %d files", len(items))
            return items
        reason = "iCloud did not report the changes in a way Homeward can rely on"
    log.info("reading the whole photo library: %s", reason)
    return None


def _plan_items(
    plan: PhotoPlan,
    items: list[PhotoItem],
    root: Path,
    records: dict[str, PhotoRecord],
    paths: _Paths,
    layout: PhotoLayout,
    progress: Progress,
    albums: bool,
) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Download what is new, restore or replace what changed. Returns the keys seen in
    iCloud and the album links wanted, as (key, album) -> primary path."""
    wanted_links: dict[tuple[str, str], str] = {}
    # key -> NAS path, for placing sidecars (next to a photo of an earlier run, too)
    assigned = {key: record.path for key, record in records.items()}
    seen: set[str] = set()
    for item in items:
        progress.advance()
        if item.key in seen:
            continue
        seen.add(item.key)
        record = records.get(item.key)
        if record is None:
            path = paths.claim(_new_path(item, layout, assigned))
            plan.actions.append(PhotoAction(PhotoActionKind.DOWNLOAD, path, item, item.key))
        else:
            path = record.path
            kind = _record_action(root, record, item)
            if kind is None:
                plan.up_to_date += 1
            else:
                plan.actions.append(PhotoAction(kind, path, item, item.key))
        assigned[item.key] = path
        if albums:
            for album in item.albums:
                wanted_links[(item.key, album)] = path
    return seen, wanted_links


def _new_path(item: PhotoItem, layout: PhotoLayout, assigned: dict[str, str]) -> PurePosixPath:
    beside = _sidecar_owner(item, assigned)
    if beside is not None:
        return beside.with_name(f"{beside.name}{PurePosixPath(item.filename).suffix}")
    return layout.folder(item) / safe_component(item.filename, "photo")


def _record_action(root: Path, record: PhotoRecord, item: PhotoItem) -> PhotoActionKind | None:
    """What a photo downloaded before needs, or None when it is up to date."""
    if not (root / record.path).exists():
        return PhotoActionKind.RESTORE
    if _changed(record, item):
        return PhotoActionKind.REPLACE
    if record.gone_at is not None:
        return PhotoActionKind.RETURNED
    return None


def _plan_links(
    plan: PhotoPlan,
    root: Path,
    layout: PhotoLayout,
    paths: _Paths,
    existing: dict[tuple[str, str], str],
    wanted_links: dict[tuple[str, str], str],
) -> None:
    """Link every photo into the folders of its albums."""
    folders = _album_folders(root, layout, existing, {key[1] for key in wanted_links})
    for (key, album), target in sorted(wanted_links.items()):
        link_path = existing.get((key, album))
        folder = folders[album]
        if link_path is not None and PurePosixPath(link_path).parent != folder:
            # The album moved to another folder (one only differing in case got renamed
            # by Synology Drive): link again there and forget the old place.
            if (root / link_path).exists():
                continue
            plan.actions.append(
                PhotoAction(PhotoActionKind.UNLINK, link_path, key=key, album=album)
            )
            link_path = None
        elif link_path is not None and (root / link_path).exists():
            continue
        if link_path is None:
            link_path = paths.claim(folder / PurePosixPath(target).name)
        plan.actions.append(
            PhotoAction(PhotoActionKind.LINK, link_path, key=key, target=target, album=album)
        )


def _plan_unlinks(
    plan: PhotoPlan,
    records: dict[str, PhotoRecord],
    existing: dict[tuple[str, str], str],
    wanted_links: dict[tuple[str, str], str],
    seen: set[str],
) -> None:
    """Remove the links of photos that left an album but are still in iCloud."""
    for (key, album), link_path in sorted(existing.items()):
        if (key, album) not in wanted_links and key in seen:
            owner = records.get(key)
            plan.actions.append(
                PhotoAction(
                    PhotoActionKind.UNLINK,
                    link_path,
                    key=key,
                    target=owner.path if owner else None,
                    album=album,
                )
            )


def _album_folders(
    root: Path,
    layout: PhotoLayout,
    existing: dict[tuple[str, str], str],
    albums: Iterable[str],
) -> dict[str, PurePosixPath]:
    """The folder of every album: where its links already are, else a new one."""
    folders = _Folders(root)
    present: dict[str, PurePosixPath] = {}
    for (_key, album), path in sorted(existing.items()):
        if album not in present and (root / path).exists():
            present[album] = PurePosixPath(path).parent
    result = {album: folders.keep(folder) for album, folder in sorted(present.items())}
    for album in sorted(set(albums) - set(result)):
        result[album] = folders.claim(layout.album_folder(album))
    return result


def _sidecar_owner(item: PhotoItem, assigned: dict[str, str]) -> PurePosixPath | None:
    """The NAS path of the file a sidecar belongs to, so ``IMG (2).HEIC`` gets
    ``IMG (2).HEIC.xmp``."""
    if item.sidecar_for is None:
        return None
    owner = replace(item, resource=item.sidecar_for).key
    return PurePosixPath(assigned[owner]) if owner in assigned else None


def _changed(record: PhotoRecord, item: PhotoItem) -> bool:
    if item.checksum is not None and record.checksum is not None:
        return item.checksum != record.checksum
    return item.size is not None and record.size is not None and item.size != record.size


class PhotoExecutor:
    """Applies a photo plan; every finished step is recorded, failed steps are retried
    by the next run."""

    def __init__(
        self,
        source: PhotoSource,
        root: Path,
        state: PhotoStateDB,
        job: str,
        *,
        today: Callable[[], date] = date.today,
        workers: int = 1,
        progress: Progress | None = None,
    ) -> None:
        self.source = source
        self.progress = progress or Progress()
        self.root = root
        self.state = state
        self.job = job
        self.workers = max(1, min(workers, MAX_WORKERS))
        self._today = today
        self._bytes = 0
        self._fetched = 0
        self._files = 0
        self._started = 0.0

    def run(self, plan: PhotoPlan) -> PhotoRunResult:
        result = PhotoRunResult()
        # Files first, so links of the same run find their target.
        order = {kind: i for i, kind in enumerate(PhotoActionKind)}
        actions = sorted(plan.actions, key=lambda a: order[a.kind])
        files = [a for a in actions if a.kind in _FILE_KINDS]
        self._bytes, self._fetched, self._files = 0, 0, len(files)
        self._started = time.monotonic()
        if files:
            sizes = [_need(a.item).size for a in files]
            known = None if None in sizes else sum(s or 0 for s in sizes)
            self.progress.phase("downloading", total=len(files), total_bytes=known or None)
        # Downloads run in parallel (each request waits mostly on Apple, not on bandwidth);
        # the state database and everything else stay on this thread.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            try:
                self._run_files(result, files, pool)
            except BaseException:
                # Cancelled: let the downloads already running finish (each one
                # writes a temporary file first), drop the queued ones.
                pool.shutdown(cancel_futures=True)
                raise
        rest = [a for a in actions if a.kind not in _FILE_KINDS]
        if rest:
            self.progress.phase("linking", total=len(rest))
        for action in rest:
            self._step(result, action, partial(self._apply, action))
        if result.ok and not plan.unchanged:
            self.state.set_cursor(self.job, plan.cursor)
            if plan.full:
                self.state.set_full_listing(self.job, plan.started_at)
        return result

    def _run_files(
        self, result: PhotoRunResult, files: list[PhotoAction], pool: ThreadPoolExecutor
    ) -> None:
        # Two batches are in flight: while the workers download one, the next one's
        # links are renewed and queued, so no worker waits for a batch to drain.
        in_flight: list[tuple[list[PhotoAction], dict[Future[int], PhotoAction]]] = []
        for start in range(0, len(files), PREPARE_BATCH):
            batch = files[start : start + PREPARE_BATCH]
            self._prepare([_need(a.item) for a in batch])
            fetches = {
                pool.submit(self._fetch, action): action
                for action in batch
                if action.kind is not PhotoActionKind.REPLACE
            }
            in_flight.append((batch, fetches))
            if len(in_flight) > 1:
                self._finish_batch(result, *in_flight.pop(0))
        for batch, fetches in in_flight:
            self._finish_batch(result, batch, fetches)

    def _finish_batch(
        self,
        result: PhotoRunResult,
        batch: list[PhotoAction],
        fetches: "dict[Future[int], PhotoAction]",
    ) -> None:
        for future in as_completed(fetches):
            action = fetches[future]
            self._step(result, action, partial(self._collect, action, future))
        for action in batch:
            if action.kind is PhotoActionKind.REPLACE:
                self._step(result, action, partial(self._apply, action))

    def _step(self, result: PhotoRunResult, action: PhotoAction, apply: Callable[[], None]) -> None:
        before = self._bytes
        try:
            apply()
        except Exception as exc:
            log.warning(
                "%s %s failed: %s", action.kind, action.path, exc, extra={"event": "file_failed"}
            )
            result.failed.append(PhotoFailure(action, f"{type(exc).__name__}: {exc}"))
            failed = True
        else:
            result.done.append(action)
            failed = False
        nbytes = self._bytes - before
        if failed and action.item is not None and action.item.size:
            nbytes = action.item.size  # keeps the remaining bytes and the estimate right
        self.progress.advance(nbytes=nbytes, failed=failed, current=action.path)
        if action.kind in _FILE_KINDS:
            self._fetched += 1
            if self._fetched % PROGRESS_EVERY == 0 or self._fetched == self._files:
                elapsed = max(time.monotonic() - self._started, 1e-6)
                log.info(
                    "%d of %d files downloaded (%.2f GB, %.1f MB/s)",
                    self._fetched, self._files, self._bytes / 1e9, self._bytes / 1e6 / elapsed,
                )  # fmt: skip

    def _prepare(self, items: list[PhotoItem]) -> None:
        try:
            self.source.prepare(items)
        except Exception as exc:  # downloads still try their URLs as listed
            log.warning("could not prepare the next downloads: %s", exc)

    def _apply(self, action: PhotoAction) -> None:
        match action.kind:
            case PhotoActionKind.DOWNLOAD | PhotoActionKind.RESTORE:
                self._download(action)
            case PhotoActionKind.REPLACE:
                old = (self.root / action.path).stat()
                self._trash(action.path)
                self._download(action)
                self._relink(action.key, action.path, old)
            case PhotoActionKind.LINK:
                target = self.root / _need(action.target)
                self._link(target, self.root / action.path)
                self.state.add_link(self.job, action.path, action.key, action.album)
            case PhotoActionKind.UNLINK:
                self._unlink(action)
                self.state.remove_link(self.job, action.path)
            case PhotoActionKind.GONE:
                self.state.mark_gone(self.job, action.key)
            case PhotoActionKind.RETURNED:
                item = _need(action.item)
                size = (self.root / action.path).stat().st_size
                self.state.record(self.job, item.key, action.path, size, item.checksum)

    def _download(self, action: PhotoAction) -> None:
        self._record(action, self._fetch(action))

    def _fetch(self, action: PhotoAction) -> int:
        """Download into place; safe to run on a worker thread (no state access)."""
        item = _need(action.item)
        target = self.root / action.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_name(target)
        try:
            with self.source.open_read(item) as stream, tmp.open("xb") as out:
                shutil.copyfileobj(stream, out, _CHUNK)
                out.flush()
                os.fsync(out.fileno())
            size = tmp.stat().st_size
            if item.size is not None and size != item.size:
                raise OSError(f"incomplete download: {size} of {item.size} bytes")
            stamp = item.created.timestamp()
            os.utime(tmp, (stamp, stamp))
            rename_noreplace(tmp, target)  # never replaces an existing file
        finally:
            tmp.unlink(missing_ok=True)
        return size

    def _collect(self, action: PhotoAction, future: "Future[int]") -> None:
        self._record(action, future.result())

    def _record(self, action: PhotoAction, size: int) -> None:
        item = _need(action.item)
        self._bytes += size
        self.state.record(self.job, item.key, action.path, size, item.checksum)

    def _trash(self, path: str) -> None:
        source = self.root / path
        day_dir = self.root / TRASH_DIR / self._today().isoformat()
        target, number = day_dir / path, 2
        while target.exists():
            target = day_dir / numbered(PurePosixPath(path), number)
            number += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def _link(self, target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link_noreplace(target, link)
        except OSError as exc:
            if link.exists():
                raise
            # File systems without hard links get a copy instead.
            log.info("hard link %s failed (%s), copying instead", link, exc)
            tmp = temp_name(link)
            try:
                shutil.copy2(target, tmp)
                rename_noreplace(tmp, link)
            finally:
                tmp.unlink(missing_ok=True)

    def _unlink(self, action: PhotoAction) -> None:
        link = self.root / action.path
        if not link.exists():
            return
        target = self.root / action.target if action.target else None
        if target is not None and target.exists() and os.path.samefile(link, target):
            link.unlink()  # the Year/Month file still holds the data
        else:
            self._trash(action.path)  # a copy or an edited file: keep it recoverable

    def _relink(self, key: str, target_path: str, old: os.stat_result) -> None:
        """After a replace, point the album links of ``key`` at the new file."""
        target = self.root / target_path
        for path, (link_key, _album) in self.state.links(self.job).items():
            if link_key != key:
                continue
            link = self.root / path
            if link.exists():
                st = link.stat()
                if (st.st_dev, st.st_ino) == (old.st_dev, old.st_ino):
                    link.unlink()  # the old version is already in the trash
                else:
                    self._trash(path)
            self._link(target, link)


def _need[T](value: T | None) -> T:
    if value is None:
        raise ValueError("photo action is missing required data")
    return value


@dataclass(frozen=True, slots=True)
class PhotoOutcome:
    plan: PhotoPlan
    result: PhotoRunResult | None  # None for a dry run


def backup_photos(
    job: str,
    source: PhotoSource,
    root: Path,
    state: PhotoStateDB,
    layout: PhotoLayout | None = None,
    *,
    albums: bool = True,
    dry_run: bool = False,
    workers: int = 1,
    today: Callable[[], date] = date.today,
    progress: Progress | None = None,
    now: Callable[[], float] = time.time,
) -> PhotoOutcome:
    plan = build_photo_plan(
        job, source, root, state, layout, albums=albums, progress=progress, now=now
    )
    if dry_run:
        return PhotoOutcome(plan, None)
    executor = PhotoExecutor(
        source, root, state, job, today=today, workers=workers, progress=progress
    )
    result = executor.run(plan)
    return PhotoOutcome(plan, result)
