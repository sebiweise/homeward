"""Helpers shared by the handlers."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

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


def package_version() -> str:
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
