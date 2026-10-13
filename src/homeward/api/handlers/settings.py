"""Global settings: how often the daemon runs every job.

The value is ``[daemon] interval_minutes``; the daemon re-reads the config before
every cycle, so a change takes effect within seconds and without a restart.
"""

from typing import Any

from homeward.api.errors import ApiError
from homeward.api.server import Context, Request
from homeward.api.store import RawConfig
from homeward.core.config import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    ConfigError,
    check_interval,
)


def get(ctx: Context, request: Request) -> dict[str, Any]:
    return _to_dict(ctx.store.load().interval_minutes)


def update(ctx: Context, request: Request) -> dict[str, Any]:
    interval = parse_interval(request.get("interval_minutes"))
    if interval is None:
        raise ApiError("missing_param", "interval_minutes")

    def change(raw: RawConfig) -> None:
        daemon = raw.setdefault("daemon", {})
        if not isinstance(daemon, dict):
            raise ApiError("config_invalid", "'daemon' must be a table")
        daemon["interval_minutes"] = interval

    return _to_dict(ctx.store.update(change).interval_minutes)


def parse_interval(value: object) -> int | None:
    """An interval from a request: a whole number of minutes, or empty/None for none."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ApiError("invalid_interval", _range()) from None
    try:
        return check_interval(value, "interval")
    except ConfigError:
        raise ApiError("invalid_interval", _range()) from None


def _range() -> str:
    return f"{MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}"


def _to_dict(interval: int) -> dict[str, Any]:
    return {
        "interval_minutes": interval,
        "default_interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_interval_minutes": MAX_INTERVAL_MINUTES,
    }
