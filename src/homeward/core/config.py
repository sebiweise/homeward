"""Configuration file (TOML) with the state directory and the sync pairs."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeward.core.models import SyncMode
from homeward.core.planner import DeleteBrake

DEFAULT_CONFIG_PATH = Path("/var/packages/homeward/var/config.toml")
REMOTE_PROVIDERS = ("icloud_drive", "folder")
DEFAULT_TRASH_DAYS = 30


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
    delete_brake: DeleteBrake = field(default_factory=DeleteBrake)
    trash_days: int = DEFAULT_TRASH_DAYS


@dataclass(frozen=True, slots=True)
class Config:
    state_dir: Path
    pairs: tuple[PairConfig, ...]

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
    return Config(state_dir=state_dir if state_dir.is_absolute() else base / state_dir,
                  pairs=parsed)  # fmt: skip


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
    trash_days = _get(entry, "trash_days", int, DEFAULT_TRASH_DAYS, where)
    if trash_days < 0:
        raise ConfigError(f"{where}: trash_days must be 0 (keep forever) or more")
    return PairConfig(
        name=name,
        local=local,
        remote=_get(entry, "remote", str, None, where),
        mode=mode,
        account=_get(entry, "account", str, "default", where),
        apple_id=_get(entry, "apple_id", str, "", where) or None,
        remote_provider=provider,
        exclude=tuple(exclude),
        delete_brake=DeleteBrake(max_ratio=float(ratio), max_count=count),
        trash_days=trash_days,
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
