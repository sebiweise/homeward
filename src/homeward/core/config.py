"""Configuration file (TOML) with the state directory and the sync pairs."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeward.core.models import SyncMode
from homeward.core.photos import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    Library,
    PhotoLayout,
    check_scheme,
)
from homeward.core.planner import DeleteBrake

DEFAULT_CONFIG_PATH = Path("/var/packages/homeward/var/config.toml")
REMOTE_PROVIDERS = ("icloud_drive", "folder")
DEFAULT_TRASH_DAYS = 30
# How often the daemon runs each job; `[daemon] interval_minutes` sets it for all jobs,
# `interval_minutes` in a [[pair]] or [[photos]] table overrides it for that job.
DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60


class ConfigError(ValueError):
    """The configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True, slots=True)
class PairConfig:
    """One NAS folder synced with one remote folder."""

    name: str
    local: Path
    remote: str
    mode: SyncMode
    account: str = "default"
    apple_id: str | None = None
    remote_provider: str = "icloud_drive"
    exclude: tuple[str, ...] = ()
    beta: bool = False
    delete_brake: DeleteBrake = field(default_factory=DeleteBrake)
    trash_days: int = DEFAULT_TRASH_DAYS
    interval_minutes: int | None = None  # None: the global interval


FOLDER_OPTIONS = (
    "shared_folder",
    "albums_folder",
    "favorites_folder",
    "hidden_folder",
    "shared_albums_folder",
)


@dataclass(frozen=True, slots=True)
class PhotosConfig:
    """One Apple ID's iCloud Photos backed up into one NAS folder."""

    name: str
    target: Path
    account: str = "default"
    apple_id: str | None = None
    libraries: tuple[Library, ...] = tuple(Library)
    albums: bool = True
    album_names: tuple[str, ...] = ()  # only these albums become folders; empty: all
    videos: bool = True
    live_videos: bool = True
    raw_alternative: bool = True  # the second file of RAW+JPEG shots
    edited: bool = True  # edited versions next to the original: "Name (bearbeitet).ext"
    favorites: bool = True  # hard links in the favorites folder
    hidden: bool = False  # hidden photos, kept in their own folder
    xmp: bool = True  # XMP sidecars with caption, keywords, location and rating
    workers: int = DEFAULT_WORKERS  # parallel downloads
    # Photos deleted in iCloud are also removed on the NAS (into the trash).
    mirror_deletes: bool = False
    delete_brake: DeleteBrake = field(default_factory=DeleteBrake)
    trash_days: int = DEFAULT_TRASH_DAYS  # with mirror_deletes: how long the trash keeps them
    layout: PhotoLayout = field(default_factory=PhotoLayout)
    interval_minutes: int | None = None  # None: the global interval


@dataclass(frozen=True, slots=True)
class Config:
    state_dir: Path
    pairs: tuple[PairConfig, ...]
    photos: tuple[PhotosConfig, ...] = ()
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES

    def photos_job(self, name: str) -> PhotosConfig:
        for job in self.photos:
            if job.name == name:
                return job
        raise ConfigError(f"no photos job named {name!r}")

    def photo_state_path(self, account: str) -> Path:
        return self.state_dir / f"{account}.photos.sqlite3"

    def pair(self, name: str) -> PairConfig:
        for pair in self.pairs:
            if pair.name == name:
                return pair
        raise ConfigError(f"no sync pair named {name!r}")

    def state_path(self, account: str) -> Path:
        return self.state_dir / f"{account}.sqlite3"


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(data, base=path.parent)


def parse_config(data: Mapping[str, Any], base: Path = Path(".")) -> Config:
    state_dir = Path(_get(data, "state_dir", str, "state"))
    pairs = data.get("pair", [])
    if not isinstance(pairs, list):
        raise ConfigError("'pair' must be a list of [[pair]] tables")
    parsed = tuple(_parse_pair(entry, i) for i, entry in enumerate(pairs, 1))
    names = [pair.name for pair in parsed]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"duplicate pair names: {', '.join(duplicates)}")
    photo_entries = data.get("photos", [])
    if not isinstance(photo_entries, list):
        raise ConfigError("'photos' must be a list of [[photos]] tables")
    photos = tuple(_parse_photos(entry, i) for i, entry in enumerate(photo_entries, 1))
    names = [job.name for job in photos]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"duplicate photos job names: {', '.join(duplicates)}")
    daemon = data.get("daemon", {})
    if not isinstance(daemon, dict):
        raise ConfigError("'daemon' must be a table")
    interval = check_interval(daemon.get("interval_minutes", DEFAULT_INTERVAL_MINUTES), "daemon")
    return Config(state_dir=state_dir if state_dir.is_absolute() else base / state_dir,
                  pairs=parsed, photos=photos, interval_minutes=interval)  # fmt: skip


def check_interval(value: object, where: str) -> int:
    """Validate a sync interval in minutes."""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_INTERVAL_MINUTES <= value <= MAX_INTERVAL_MINUTES
    ):
        raise ConfigError(
            f"{where}: 'interval_minutes' must be a whole number from "
            f"{MIN_INTERVAL_MINUTES} to {MAX_INTERVAL_MINUTES}"
        )
    return value


def _get_interval(table: Mapping[str, Any], where: str) -> int | None:
    value = table.get("interval_minutes")
    return None if value is None else check_interval(value, where)


def _parse_photos(entry: object, index: int) -> PhotosConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"photos #{index} must be a table")
    name = _get(entry, "name", str, None, f"photos #{index}")
    where = f"photos {name!r}"
    target = Path(_get(entry, "target", str, None, where))
    if not target.is_absolute():
        raise ConfigError(f"{where}: target must be an absolute path")
    values = _get(entry, "libraries", list, [lib.value for lib in Library], where)
    try:
        libraries = tuple(Library(value) for value in values)
    except ValueError:
        choices = ", ".join(lib.value for lib in Library)
        raise ConfigError(f"{where}: libraries may only contain {choices}") from None
    if not libraries:
        raise ConfigError(f"{where}: libraries must not be empty")
    defaults = PhotoLayout()
    scheme = _get(entry, "folder_scheme", str, defaults.scheme, where)
    try:
        check_scheme(scheme)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    apple_id = _get(entry, "apple_id", str, "", where) or None
    workers = entry.get("workers", DEFAULT_WORKERS)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS:
        raise ConfigError(f"{where}: 'workers' must be a whole number from 1 to {MAX_WORKERS}")
    brake, trash_days = _brake_and_trash(entry, where)
    album_names = _get(entry, "album_names", list, [], where)
    if not all(isinstance(n, str) for n in album_names):
        raise ConfigError(f"{where}: album_names must be a list of strings")
    return PhotosConfig(
        name=name,
        target=target,
        account=_get(entry, "account", str, "default", where),
        apple_id=apple_id,
        libraries=libraries,
        albums=_get_bool(entry, "albums", True, where),
        album_names=tuple(album_names),
        videos=_get_bool(entry, "videos", True, where),
        live_videos=_get_bool(entry, "live_videos", True, where),
        raw_alternative=_get_bool(entry, "raw_alternative", True, where),
        edited=_get_bool(entry, "edited", True, where),
        favorites=_get_bool(entry, "favorites", True, where),
        hidden=_get_bool(entry, "hidden", False, where),
        xmp=_get_bool(entry, "xmp", True, where),
        workers=workers,
        mirror_deletes=_get_bool(entry, "mirror_deletes", False, where),
        delete_brake=brake,
        trash_days=trash_days,
        interval_minutes=_get_interval(entry, where),
        layout=PhotoLayout(
            scheme=scheme,
            source_folders=_get_bool(entry, "source_folders", True, where),
            **{
                name: _get(entry, name, str, getattr(defaults, name), where)
                for name in FOLDER_OPTIONS
            },
        ),
    )


def _get_bool(table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be true or false")
    return value


def _parse_pair(entry: object, index: int) -> PairConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"pair #{index} must be a table")
    where = f"pair #{index}"
    name = _get(entry, "name", str, None, where)
    where = f"pair {name!r}"
    mode_value = _get(entry, "mode", str, None, where)
    try:
        mode = SyncMode(mode_value)
    except ValueError:
        choices = ", ".join(m.value for m in SyncMode)
        raise ConfigError(f"{where}: mode must be one of {choices}") from None
    beta = entry.get("beta", False)
    if not isinstance(beta, bool):
        raise ConfigError(f"{where}: 'beta' must be true or false")
    if mode is SyncMode.BIDIRECTIONAL and not beta:
        raise ConfigError(
            f"{where}: bidirectional sync is in beta; set beta = true to enable it for this pair"
        )
    provider = _get(entry, "remote_provider", str, "icloud_drive", where)
    if provider not in REMOTE_PROVIDERS:
        raise ConfigError(f"{where}: remote_provider must be one of {', '.join(REMOTE_PROVIDERS)}")
    exclude = _get(entry, "exclude", list, [], where)
    if not all(isinstance(p, str) for p in exclude):
        raise ConfigError(f"{where}: exclude must be a list of strings")
    local = Path(_get(entry, "local", str, None, where))
    if not local.is_absolute():
        raise ConfigError(f"{where}: local must be an absolute path")
    brake, trash_days = _brake_and_trash(entry, where)
    return PairConfig(
        name=name,
        local=local,
        remote=_get(entry, "remote", str, None, where),
        mode=mode,
        account=_get(entry, "account", str, "default", where),
        apple_id=_get(entry, "apple_id", str, "", where) or None,
        remote_provider=provider,
        exclude=tuple(exclude),
        beta=beta,
        delete_brake=brake,
        trash_days=trash_days,
        interval_minutes=_get_interval(entry, where),
    )


def _brake_and_trash(entry: dict[str, Any], where: str) -> tuple[DeleteBrake, int]:
    defaults = DeleteBrake()
    ratio = _get(entry, "max_delete_ratio", (int, float), defaults.max_ratio, where)
    count = _get(entry, "max_delete_count", int, defaults.max_count, where)
    trash_days = _get(entry, "trash_days", int, DEFAULT_TRASH_DAYS, where)
    if trash_days < 0:
        raise ConfigError(f"{where}: trash_days must be 0 (keep forever) or more")
    return DeleteBrake(max_ratio=float(ratio), max_count=count), trash_days


def _get(
    table: Mapping[str, Any],
    key: str,
    kind: type | tuple[type, ...],
    default: Any,
    where: str = "config",
) -> Any:
    if key not in table:
        if default is None:
            raise ConfigError(f"{where}: '{key}' is required")
        return default
    value = table[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' has the wrong type")
    return value
