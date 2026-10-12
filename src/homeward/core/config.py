"""Configuration file (TOML) with the state directory and the sync pairs."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeward.core.models import SyncMode
from homeward.core.photos import Library, PhotoLayout, check_scheme
from homeward.core.planner import DeleteBrake

DEFAULT_CONFIG_PATH = Path("/var/packages/homeward/var/config.toml")
REMOTE_PROVIDERS = ("icloud_drive", "folder")


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
    remote_provider: str = "icloud_drive"
    exclude: tuple[str, ...] = ()
    delete_brake: DeleteBrake = field(default_factory=DeleteBrake)


@dataclass(frozen=True, slots=True)
class PhotosConfig:
    """One Apple ID's iCloud Photos backed up into one NAS folder."""

    name: str
    target: Path
    account: str = "default"
    apple_id: str | None = None
    libraries: tuple[Library, ...] = (Library.PERSONAL, Library.SHARED)
    albums: bool = True
    videos: bool = True
    live_videos: bool = True
    layout: PhotoLayout = field(default_factory=PhotoLayout)


@dataclass(frozen=True, slots=True)
class Config:
    state_dir: Path
    pairs: tuple[PairConfig, ...]
    photos: tuple[PhotosConfig, ...] = ()

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
    return Config(state_dir=state_dir if state_dir.is_absolute() else base / state_dir,
                  pairs=parsed, photos=photos)  # fmt: skip


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
    return PhotosConfig(
        name=name,
        target=target,
        account=_get(entry, "account", str, "default", where),
        apple_id=apple_id,
        libraries=libraries,
        albums=_get_bool(entry, "albums", True, where),
        videos=_get_bool(entry, "videos", True, where),
        live_videos=_get_bool(entry, "live_videos", True, where),
        layout=PhotoLayout(
            scheme=scheme,
            shared_folder=_get(entry, "shared_folder", str, defaults.shared_folder, where),
            albums_folder=_get(entry, "albums_folder", str, defaults.albums_folder, where),
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
    provider = _get(entry, "remote_provider", str, "icloud_drive", where)
    if provider not in REMOTE_PROVIDERS:
        raise ConfigError(f"{where}: remote_provider must be one of {', '.join(REMOTE_PROVIDERS)}")
    exclude = _get(entry, "exclude", list, [], where)
    if not all(isinstance(p, str) for p in exclude):
        raise ConfigError(f"{where}: exclude must be a list of strings")
    local = Path(_get(entry, "local", str, None, where))
    if not local.is_absolute():
        raise ConfigError(f"{where}: local must be an absolute path")
    defaults = DeleteBrake()
    ratio = _get(entry, "max_delete_ratio", (int, float), defaults.max_ratio, where)
    count = _get(entry, "max_delete_count", int, defaults.max_count, where)
    return PairConfig(
        name=name,
        local=local,
        remote=_get(entry, "remote", str, None, where),
        mode=mode,
        account=_get(entry, "account", str, "default", where),
        remote_provider=provider,
        exclude=tuple(exclude),
        delete_brake=DeleteBrake(max_ratio=float(ratio), max_count=count),
    )


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
