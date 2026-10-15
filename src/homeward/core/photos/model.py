"""Types of a photos job: the iCloud side, the NAS layout, plans and results."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol

CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f/\\:]+")
_RESERVED_PREFIXES = (".", "@", "#")
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
            album = PurePosixPath(safe_name(item.collection or "", "_"))
            if not self.source_folders:
                return album
            return PurePosixPath(safe_name(self.shared_albums_folder)) / album
        when = item.created.astimezone()
        parts = self.scheme.format(
            year=f"{when.year:04d}", month=f"{when.month:02d}", day=f"{when.day:02d}"
        )
        folder = safe_relative(parts)
        if item.library is Library.SHARED and self.source_folders:
            folder = PurePosixPath(safe_name(self.shared_folder)) / folder
        if item.hidden:
            folder = PurePosixPath(safe_name(self.hidden_folder)) / folder
        return folder

    def album_folder(self, album: str) -> PurePosixPath:
        if album == FAVORITES_ALBUM:
            return PurePosixPath(safe_name(self.favorites_folder))
        return PurePosixPath(safe_name(self.albums_folder)) / safe_relative(album)


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


def safe_name(value: str, fallback: str = "_") -> str:
    """A folder or file name in the backup: also no hidden names (``.x``) and none DSM
    treats as its own (``@eaDir``, ``#recycle``), which Synology Photos and File
    Station would hide or the recycle bin would claim. Those get a ``_`` in front."""
    cleaned = safe_component(value, fallback)
    return f"_{cleaned}" if cleaned.startswith(_RESERVED_PREFIXES) else cleaned


def safe_relative(value: str) -> PurePosixPath:
    parts = [safe_name(p) for p in value.replace("\\", "/").split("/") if p not in {"", "."}]
    return PurePosixPath(*[p for p in parts if p != ".."]) if parts else PurePosixPath()


class PhotoActionKind(StrEnum):
    DOWNLOAD = "download"  # new photo
    RESTORE = "restore"  # known photo whose NAS file is missing
    REPLACE = "replace"  # the original changed in iCloud; old file goes to the trash
    LINK = "link"  # add a photo to an album folder
    UNLINK = "unlink"  # the photo left the album; its Year/Month file stays
    GONE = "gone"  # deleted in iCloud; the NAS copy stays
    RETURNED = "returned"  # back in iCloud after it was gone (e.g. restored there)
    DELETE = "delete"  # deleted in iCloud, mirrored: NAS file and album links to the trash


FILE_KINDS = frozenset({PhotoActionKind.DOWNLOAD, PhotoActionKind.RESTORE, PhotoActionKind.REPLACE})
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
    selection: str | None = None  # see ``build_photo_plan``
    blocked: str | None = None  # the delete brake holds back the DELETE actions...
    held: set[str] = field(default_factory=set)  # ...of these photo keys
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
    blocked: str | None = None  # deletions held back by the delete brake

    @property
    def ok(self) -> bool:
        return not self.failed
