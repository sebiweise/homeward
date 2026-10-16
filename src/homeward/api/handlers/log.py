"""The daemon's log file and the recorded sync runs."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import event_dict, existing_state
from homeward.api.server import Context, Request
from homeward.runtime import Event, Level, read_events

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


# Codes the daemon writes when a run ends (with ``exit_code``), see daemon.scheduler.
RUN_END = frozenset(
    {"sync_finished", "sync_failed", "sync_stopped", "sync_cancelled", "sync_postponed",
     "delete_brake", "config_error"}
)  # fmt: skip
MAX_HISTORY_EVENTS = 20_000


def history(ctx: Context, request: Request) -> dict[str, Any]:
    """The runs of one pair or photos job (``source``), newest first, built from the
    event log: when each started and ended, how it ended, what it did and every wait,
    retry or error in between."""
    source = request.get("source")
    limit = _int(request.get("limit", "30"), 1, 200)
    found = [e for e in read_events(ctx.paths, limit=MAX_HISTORY_EVENTS) if e.source == source]
    runs = _Runs()
    for event in reversed(found):  # oldest first
        runs.add(event)
    return {"source": source, "runs": runs.runs[::-1][:limit]}


class _Runs:
    """Groups one job's events, oldest first, into runs."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def add(self, event: Event) -> None:
        if event.code == "sync_started":
            self.current = _new_run(event.time, event.details)
            self.runs.append(self.current)
        elif event.code in RUN_END and "exit_code" in event.details:
            self._end(event)
        elif self.current is not None:
            if event.code == "sync_summary":
                self.current["counts"] = event.details.get("counts") or {}
                self.current["files"] = event.details.get("files") or []
                self.current["files_total"] = event.details.get("files_total", 0)
            else:
                self.current["events"].append(event_dict(event))

    def _end(self, event: Event) -> None:
        run = self.current
        if run is None:  # its start is older than the event log
            run = _new_run(None, event.details)
            self.runs.append(run)
        run |= {"finished_at": event.time, "result": event.code,
                "level": event.level, "message": event.message}  # fmt: skip
        self.current = None


def _new_run(started_at: float | None, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "finished_at": None,
        "result": None,  # None while it runs, or if the service stopped without a word
        "level": None,
        "message": "",
        "dry_run": bool(details.get("dry_run")),
        "counts": None,
        "files": [],  # deleted, replaced, conflicting or moved files: kind, path, to
        "files_total": 0,
        "events": [],
    }


def events(ctx: Context, request: Request) -> dict[str, Any]:
    """The event log, newest first; ``level`` hides less important events and
    ``source`` limits it to one pair, photos job or the service."""
    limit = _int(request.get("limit", "200"), 1, 2000)
    order = list(Level)
    try:
        minimum = order.index(Level(request.get("level", Level.INFO)))
    except ValueError:
        raise ApiError("invalid_param", "level") from None
    source = request.get("source", "")
    found = [
        e
        for e in read_events(ctx.paths, limit=10_000)
        if order.index(e.level) >= minimum and (not source or e.source == source)
    ]
    return {"events": [event_dict(event) for event in found[:limit]]}


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
