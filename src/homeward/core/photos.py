"""iCloud Photos backup (iCloud -> NAS only): plan and apply one run of a photos job.

Every original lands once below ``<target>/<scheme>/`` (Year/Month by default); the
shared library goes below its own folder. Album folders hold hard links to those
files, so a photo in several albums takes its disk space once.

A backup never deletes: photos removed in iCloud stay on the NAS, a changed original
moves the old file to the trash first, and only album links (never the last copy of
a photo) are removed when a photo leaves an album.
"""

import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from homeward.core.photo_state import PhotoRecord, PhotoStateDB
from homeward.core.rules import TEMP_SUFFIX, TRASH_DIR

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f/\\:]+")
SCHEME_FIELDS = ("year", "month", "day")


class Library(StrEnum):
    PERSONAL = "personal"
    SHARED = "shared"


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

    @property
    def key(self) -> str:
        return f"{self.library}/{self.asset_id}/{self.resource}"


class PhotoSource(Protocol):
    """The iCloud side of a photos job, implemented in ``homeward.providers``."""

    name: str

    def cursor(self) -> str | None:
        """An opaque token that changes whenever anything in the libraries changes."""
        ...

    def list_items(self) -> Iterable[PhotoItem]:
        """Every file of every photo in the selected libraries."""
        ...

    def open_read(self, item: PhotoItem) -> BinaryIO:
        """Stream the original. The caller closes it."""
        ...


@dataclass(frozen=True, slots=True)
class PhotoLayout:
    scheme: str = "{year}/{month}"
    shared_folder: str = "Geteilte Mediathek"
    albums_folder: str = "Alben"

    def folder(self, item: PhotoItem) -> PurePosixPath:
        when = item.created.astimezone()
        parts = self.scheme.format(
            year=f"{when.year:04d}", month=f"{when.month:02d}", day=f"{when.day:02d}"
        )
        folder = safe_relative(parts)
        if item.library is Library.SHARED:
            folder = PurePosixPath(safe_component(self.shared_folder)) / folder
        return folder

    def album_folder(self, album: str) -> PurePosixPath:
        return PurePosixPath(safe_component(self.albums_folder)) / safe_relative(album)


def check_scheme(scheme: str) -> None:
    """Raise ValueError unless ``scheme`` only uses ``{year}``, ``{month}`` and ``{day}``."""
    try:
        rendered = scheme.format(**{name: "0" for name in SCHEME_FIELDS})
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
    """Hands out NAS paths that no other photo or link owns and that are free on disk."""

    def __init__(self, root: Path, owned: Iterable[str]) -> None:
        self.root = root
        self.taken = set(owned)

    def claim(self, wanted: PurePosixPath) -> str:
        candidate, number = wanted, 2
        while str(candidate) in self.taken or (self.root / candidate).exists():
            candidate = numbered(wanted, number)
            number += 1
        self.taken.add(str(candidate))
        return str(candidate)


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
) -> PhotoPlan:
    """Compare iCloud with the state of the last run and decide what to fetch or link."""
    layout = layout or PhotoLayout()
    records = state.records(job)
    links = state.links(job)
    cursor = source.cursor()
    plan = PhotoPlan(job=job, cursor=cursor)
    if (
        cursor is not None
        and cursor == state.cursor(job)
        and _all_present(root, records.values(), links)
    ):
        plan.unchanged = True
        plan.up_to_date = sum(1 for r in records.values() if r.gone_at is None)
        return plan

    paths = _Paths(root, [r.path for r in records.values()] + list(links))
    wanted_links: dict[tuple[str, str], str] = {}  # (key, album) -> primary path
    seen: set[str] = set()
    for item in sorted(source.list_items(), key=lambda i: (i.created, i.key)):
        if item.key in seen:
            continue
        seen.add(item.key)
        record = records.get(item.key)
        if record is None:
            wanted = layout.folder(item) / safe_component(item.filename, "photo")
            path = paths.claim(wanted)
            plan.actions.append(PhotoAction(PhotoActionKind.DOWNLOAD, path, item, item.key))
        else:
            path = record.path
            if not (root / path).exists():
                plan.actions.append(PhotoAction(PhotoActionKind.RESTORE, path, item, item.key))
            elif _changed(record, item):
                plan.actions.append(PhotoAction(PhotoActionKind.REPLACE, path, item, item.key))
            elif record.gone_at is not None:
                plan.actions.append(PhotoAction(PhotoActionKind.RETURNED, path, item, item.key))
            else:
                plan.up_to_date += 1
        if albums:
            for album in item.albums:
                wanted_links[(item.key, album)] = path

    for key, record in sorted(records.items()):
        if key not in seen and record.gone_at is None:
            plan.actions.append(PhotoAction(PhotoActionKind.GONE, record.path, key=key))

    existing = {(key, album): path for path, (key, album) in links.items()}
    for (key, album), target in sorted(wanted_links.items()):
        link_path = existing.get((key, album))
        if link_path is not None and (root / link_path).exists():
            continue
        if link_path is None:
            name = PurePosixPath(target).name
            link_path = paths.claim(layout.album_folder(album) / name)
        plan.actions.append(
            PhotoAction(PhotoActionKind.LINK, link_path, key=key, target=target, album=album)
        )
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
    return plan


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
    ) -> None:
        self.source = source
        self.root = root
        self.state = state
        self.job = job
        self._today = today

    def run(self, plan: PhotoPlan) -> PhotoRunResult:
        result = PhotoRunResult()
        # Files first, so links of the same run find their target.
        order = {kind: i for i, kind in enumerate(PhotoActionKind)}
        files = sum(1 for a in plan.actions if a.kind in _FILE_KINDS)
        fetched = 0
        for action in sorted(plan.actions, key=lambda a: order[a.kind]):
            try:
                self._apply(action)
            except Exception as exc:
                log.warning("%s %s failed: %s", action.kind, action.path, exc)
                result.failed.append(PhotoFailure(action, f"{type(exc).__name__}: {exc}"))
            else:
                result.done.append(action)
            if action.kind in _FILE_KINDS:
                fetched += 1
                if fetched % PROGRESS_EVERY == 0 or fetched == files:
                    log.info("%d of %d files downloaded", fetched, files)
        if result.ok and not plan.unchanged:
            self.state.set_cursor(self.job, plan.cursor)
        return result

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
        item = _need(action.item)
        target = self.root / action.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
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
            os.link(tmp, target)  # never replaces an existing file
        finally:
            tmp.unlink(missing_ok=True)
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
            os.link(target, link)
        except OSError as exc:
            if link.exists():
                raise
            # File systems without hard links get a copy instead.
            log.info("hard link %s failed (%s), copying instead", link, exc)
            tmp = link.with_name(f".{link.name}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
            try:
                shutil.copy2(target, tmp)
                os.link(tmp, link)
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
    today: Callable[[], date] = date.today,
) -> PhotoOutcome:
    plan = build_photo_plan(job, source, root, state, layout, albums=albums)
    if dry_run:
        return PhotoOutcome(plan, None)
    result = PhotoExecutor(source, root, state, job, today=today).run(plan)
    return PhotoOutcome(plan, result)
