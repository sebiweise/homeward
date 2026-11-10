"""Helpers shared by the handlers."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.folders import check_shared_folder
from homeward.api.server import Context
from homeward.apple_errors import classify
from homeward.core.config import PACKAGE_DIR, Config, in_system_folder
from homeward.core.state import StateDB
from homeward.core.workflow import DEFAULT_ARCHIVE_LIMIT
from homeward.runtime import Event, Level

# Pair and account names become file names (run requests, state databases).
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@-]{0,63}$")


def check_name(value: str, what: str) -> str:
    value = value.strip()
    if not _NAME.match(value):
        raise ApiError("invalid_name", f"invalid {what} name: {value!r}")
    return value


def check_nas_folder(ctx: Context, value: str, key: str) -> Path:
    """A NAS folder a pair or photos backup may use: absolute, without ``..``, none of
    the hidden folders (``core.config.HIDDEN_FOLDERS``) on the path or on where its
    symlinks lead, and neither inside nor around the package's own data (config,
    Apple ID sessions, state), which the daemon could otherwise read or
    overwrite: a sync of a folder that contains it would copy the sessions too. On
    DSM it must also lie inside a shared folder (``folders.check_shared_folder``)."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ApiError("invalid_param", key)
    real = path.resolve()
    # Also where a symlink leads: a share link into @appstore/homeward or @appdata.
    if in_system_folder(path) or in_system_folder(real):
        raise ApiError("invalid_param", key)
    private = [f.resolve() for f in (PACKAGE_DIR, ctx.paths.var_dir, ctx.store.load().state_dir)]
    if any(real.is_relative_to(folder) or folder.is_relative_to(real) for folder in private):
        raise ApiError("invalid_param", key)
    check_shared_folder(path, key, ctx.paths)
    return path


def without_none(table: dict[str, Any]) -> dict[str, Any]:
    """Drop unset optional keys (TOML has no null), e.g. an interval left to the default."""
    return {key: value for key, value in table.items() if value is not None}


DSM_INFO = Path("/var/packages/homeward/INFO")


def package_version(info: Path = DSM_INFO) -> str:
    """The version Package Center shows, else the installed Python package's."""
    try:
        for line in info.read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                return line.partition("=")[2].strip().strip('"')
    except OSError:
        pass
    try:
        return version("homeward")
    except PackageNotFoundError:
        return "0.0.0"


@contextmanager
def existing_state(config: Config, account: str) -> Iterator[StateDB | None]:
    """Open an account's state database only if it exists (reads never create one)."""
    path = config.state_path(account)
    if not path.exists():
        yield None
        return
    with StateDB(path) as state:
        yield state


def event_dict(event: Event) -> dict[str, Any]:
    """An event for the UI; a warning or error from Apple gets the ``hint`` code of
    a text that explains it (see ``homeward.apple_errors``)."""
    result = asdict(event)
    if event.level != Level.INFO and event.message:
        result["hint"] = classify(event.message)
    return result


def parse_rules(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A job's file rules as config tables; empty values are left out."""
    rules = data.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise ApiError("invalid_param", "rules")
    tables = []
    for rule in rules:
        table = {k: v for k, v in rule.items() if v is not None and v != "" and v != []}
        if isinstance(table.get("name"), str):
            table["name"] = table["name"].strip()
        tables.append(table)
    return tables


def parse_archive_limit(data: dict[str, Any]) -> int:
    limit = data.get("archive_limit", DEFAULT_ARCHIVE_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ApiError("invalid_param", "archive_limit")
    return limit
