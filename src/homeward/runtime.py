"""Files the UI API and the background daemon share below the package's var folder.

- ``config.toml``: accounts and sync pairs (the UI writes it, the daemon reads it)
- ``requests/<pair>.json``: "run now" requests from the UI, consumed by the daemon
- ``cancel/<pair>.json``: "cancel" requests from the UI, consumed by the daemon
- ``status.json``: what the daemon is doing right now, written by the daemon
- ``homeward.log``: the daemon's log, shown in the UI
- ``events.jsonl``: the event log (waits, retries, errors) shown in the UI
- ``notices.json``: when the user last cleared the notices on the status page
- ``progress/<job>.json``: how far the running sync or photos backup is, written by it
"""

import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_VAR_DIR = Path("/var/packages/homeward/var")
MAX_EVENTS_BYTES = 2 * 1024 * 1024  # then events.jsonl moves to events.jsonl.1

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Paths:
    var_dir: Path
    config: Path

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Paths":
        env = os.environ if environ is None else environ
        var_dir = Path(env.get("HOMEWARD_VAR", DEFAULT_VAR_DIR))
        config = Path(env.get("HOMEWARD_CONFIG", var_dir / "config.toml"))
        return cls(var_dir=var_dir, config=config)

    @property
    def requests_dir(self) -> Path:
        return self.var_dir / "requests"

    @property
    def cancel_dir(self) -> Path:
        return self.var_dir / "cancel"

    @property
    def status_file(self) -> Path:
        return self.var_dir / "status.json"

    @property
    def log_file(self) -> Path:
        return self.var_dir / "homeward.log"

    @property
    def events_file(self) -> Path:
        return self.var_dir / "events.jsonl"

    @property
    def notices_file(self) -> Path:
        return self.var_dir / "notices.json"

    @property
    def progress_dir(self) -> Path:
        return self.var_dir / "progress"

    def progress_file(self, source: str) -> Path:
        """``source`` is ``pair:<name>`` or ``photos:<name>`` (names are file-safe)."""
        return self.progress_dir / (source.replace(":", "-", 1) + ".json")

    @property
    def backup_dir(self) -> Path:
        return self.var_dir / "config-backups"


@dataclass(frozen=True, slots=True)
class RunRequest:
    pair: str
    dry_run: bool = False
    allow_deletes: bool = False
    remove_gone: bool = False  # photos: also clean up photos deleted in iCloud
    requested_at: float = 0.0


def write_json_atomic(path: Path, data: Any, mode: int = 0o640) -> None:
    """Write JSON so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def request_run(paths: Paths, request: RunRequest) -> None:
    """Ask the daemon to sync one pair as soon as possible."""
    data = {
        "pair": request.pair,
        "dry_run": request.dry_run,
        "allow_deletes": request.allow_deletes,
        "remove_gone": request.remove_gone,
        "requested_at": request.requested_at or time.time(),
    }
    write_json_atomic(paths.requests_dir / f"{request.pair}.json", data)


def pending_requests(paths: Paths) -> list[RunRequest]:
    return [request for _, request in _read_requests(paths)]


def take_requests(paths: Paths) -> list[RunRequest]:
    """Return and remove all pending run requests (for the daemon)."""
    taken = []
    for file, request in _read_requests(paths):
        file.unlink(missing_ok=True)
        taken.append(request)
    return taken


def _read_requests(paths: Paths) -> Iterator[tuple[Path, RunRequest]]:
    if not paths.requests_dir.is_dir():
        return
    for file in sorted(paths.requests_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            request = RunRequest(
                pair=str(data["pair"]),
                dry_run=bool(data.get("dry_run", False)),
                allow_deletes=bool(data.get("allow_deletes", False)),
                remove_gone=bool(data.get("remove_gone", False)),
                requested_at=float(data.get("requested_at", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        yield file, request


def drop_request(paths: Paths, pair: str) -> bool:
    """Remove a pending run request; False when there was none."""
    try:
        (paths.requests_dir / f"{pair}.json").unlink()
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """Stop the running sync of ``pair`` (a pair name or ``photos:<job>``)."""

    pair: str
    requested_at: float = 0.0


def request_cancel(paths: Paths, request: CancelRequest) -> None:
    """Ask the daemon to stop the running sync of one pair or photos job."""
    data = {"pair": request.pair, "requested_at": request.requested_at or time.time()}
    write_json_atomic(paths.cancel_dir / f"{request.pair}.json", data)


def take_cancels(paths: Paths) -> list[CancelRequest]:
    """Return and remove all cancel requests (for the daemon)."""
    if not paths.cancel_dir.is_dir():
        return []
    taken = []
    for file in sorted(paths.cancel_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            taken.append(CancelRequest(str(data["pair"]), float(data.get("requested_at", 0.0))))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        file.unlink(missing_ok=True)
    return taken


def write_progress(paths: Paths, source: str, snapshot: dict[str, Any]) -> None:
    write_json_atomic(paths.progress_file(source), snapshot | {"pid": os.getpid()})


def clear_progress(paths: Paths, source: str) -> None:
    paths.progress_file(source).unlink(missing_ok=True)


def read_progress(paths: Paths, source: str) -> dict[str, Any] | None:
    """The progress of a running job, or None when there is none or its run ended."""
    try:
        data = json.loads(paths.progress_file(source).read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)  # the run crashed without removing it
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # the process exists
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def read_status(paths: Paths) -> dict[str, Any]:
    """The daemon's status file, or an empty dict when there is none (yet)."""
    try:
        data = json.loads(paths.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Codes the UI has texts for; producers may use others, the UI then shows ``message``.
EVENT_CODES = (
    "icloud_indexing",  # Apple still prepares the photo library; set ``until``
    "rate_limited",  # Apple throttles requests; set ``until``
    "apple_unavailable",  # 5xx from Apple, retried later; set ``until`` if known
    "link_expired",  # a download URL expired and is fetched again
    "retry",  # a transfer failed and will be retried
    "login_required",  # the Apple ID session expired
    "sync_started",
    "sync_finished",
    "sync_failed",
    "sync_stopped",  # the service stopped (e.g. package update); the run is repeated
    "sync_cancelled",  # the user cancelled the run in the DSM UI
    "sync_paused",  # the job's time window closed; it goes on when the window opens
    "sync_postponed",  # iCloud was not ready (e.g. indexing); the next scheduled run retries
    "delete_brake",
    "conflict",
    "file_failed",
    "config_error",  # the config file is invalid; nothing runs until it is fixed
    "already_running",  # another run for the same account still holds the lock
    "folder_missing",  # a synced folder is gone; the run stopped before changing anything
    "integrity_repaired",  # the weekly check moved a damaged photo aside to download it again
    "integrity_damaged",  # the weekly check found a damaged photo iCloud no longer has
    "not_backed_up",  # no successful run for ``STALE_DAYS``; ``days`` says how long
    "notices_cleared",  # the user cleared the notices on the status page
    "sync_summary",  # what a run did; ``counts`` maps an action kind to a number
)


class Level(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    """Something the user should be able to see happen in the background.

    ``source`` names what it is about (``photos:<name>``, ``pair:<name>``,
    ``account:<name>`` or ``service``). ``code`` is a stable key the UI translates
    (``[event]`` section of the strings), ``message`` is English detail for the log.
    ``until`` is set while Homeward deliberately waits, e.g. for Apple to finish
    indexing or to lift a rate limit.
    """

    time: float
    level: Level
    source: str
    code: str
    message: str = ""
    until: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


def record_event(
    level: Level,
    source: str,
    code: str,
    message: str = "",
    *,
    until: float | None = None,
    paths: Paths | None = None,
    **details: Any,
) -> None:
    """Append one event to the event log. Never raises: events must not break a sync."""
    paths = paths or Paths.from_env()
    event = Event(time.time(), Level(level), source, code, message, until, details)
    line = json.dumps(asdict(event), ensure_ascii=False, default=str) + "\n"
    try:
        paths.events_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if paths.events_file.stat().st_size > MAX_EVENTS_BYTES:
                os.replace(paths.events_file, paths.events_file.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        fd = os.open(paths.events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        log.warning("could not record event %s %s: %s", source, code, exc)


def read_events(paths: Paths, limit: int = 200) -> list[Event]:
    """The newest events first, from the current and the rotated event log."""
    events: list[Event] = []
    for file in (paths.events_file, paths.events_file.with_suffix(".jsonl.1")):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                data = json.loads(line)
                events.append(
                    Event(
                        time=float(data["time"]),
                        level=Level(data["level"]),
                        source=str(data["source"]),
                        code=str(data["code"]),
                        message=str(data.get("message", "")),
                        until=None if data.get("until") is None else float(data["until"]),
                        details=dict(data.get("details") or {}),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
            if len(events) >= limit:
                return events
    return events


class EventLogHandler(logging.Handler):
    """Turns log records of one run into events.

    Every warning and error becomes an event; info records only when they carry an
    ``event`` code, e.g. ``log.info("...", extra={"event": "icloud_indexing",
    "until": time.time() + 60})``.
    """

    def __init__(self, source: str, paths: Paths) -> None:
        super().__init__(logging.INFO)
        self.source = source
        self.paths = paths

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return  # never turn a failure to record an event into another event
        code = getattr(record, "event", None)
        if code is None and record.levelno < logging.WARNING:
            return
        if record.levelno >= logging.ERROR:
            level = Level.ERROR
        elif record.levelno >= logging.WARNING:
            level = Level.WARNING
        else:
            level = Level.INFO
        until = getattr(record, "until", None)
        keys = ("account", "counts", "files", "files_total")
        details = {key: getattr(record, key) for key in keys if hasattr(record, key)}
        record_event(
            level,
            self.source,
            str(code or "log"),
            record.getMessage(),
            until=float(until) if until is not None else None,
            paths=self.paths,
            **details,
        )
