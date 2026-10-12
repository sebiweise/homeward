"""The daemon's log file and the recorded sync runs."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import existing_state
from homeward.api.server import Context, Request
from homeward.runtime import Level, read_events

MAX_LINES = 2000
_CHUNK = 8192


def tail(ctx: Context, request: Request) -> dict[str, Any]:
    lines = _int(request.get("lines", "200"), 1, MAX_LINES)
    path = ctx.paths.log_file
    if not path.exists():
        return {"lines": []}
    return {"lines": _last_lines(path, lines)}


def runs(ctx: Context, request: Request) -> dict[str, Any]:
    limit = _int(request.get("limit", "100"), 1, 1000)
    config = ctx.store.load()
    result: list[dict[str, Any]] = []
    for account in sorted({pair.account for pair in config.pairs}):
        with existing_state(config, account) as state:
            if state:
                result.extend(asdict(run) for run in state.runs(limit=limit))
    result.sort(key=lambda run: run["started_at"], reverse=True)
    return {"runs": result[:limit]}


def events(ctx: Context, request: Request) -> dict[str, Any]:
    """The event log, newest first; ``level`` hides less important events."""
    limit = _int(request.get("limit", "200"), 1, 2000)
    order = list(Level)
    try:
        minimum = order.index(Level(request.get("level", Level.INFO)))
    except ValueError:
        raise ApiError("invalid_param", "level") from None
    found = [e for e in read_events(ctx.paths, limit=10_000) if order.index(e.level) >= minimum]
    return {"events": [asdict(event) for event in found[:limit]]}


def _int(value: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ApiError("invalid_param", value) from None
    return max(low, min(high, number))


def _last_lines(path: Path, count: int) -> list[str]:
    """Read the last ``count`` lines without loading a large log completely."""
    with path.open("rb") as fh:
        end = fh.seek(0, 2)
        data = b""
        while end > 0 and data.count(b"\n") <= count:
            start = max(0, end - _CHUNK)
            fh.seek(start)
            data = fh.read(end - start) + data
            end = start
    return data.decode("utf-8", errors="replace").splitlines()[-count:]
