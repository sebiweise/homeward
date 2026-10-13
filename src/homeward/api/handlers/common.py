"""Helpers shared by the handlers."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.core.config import Config
from homeward.core.state import StateDB

# Pair and account names become file names (run requests, state databases).
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@-]{0,63}$")


def check_name(value: str, what: str) -> str:
    value = value.strip()
    if not _NAME.match(value):
        raise ApiError("invalid_name", f"invalid {what} name: {value!r}")
    return value


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
