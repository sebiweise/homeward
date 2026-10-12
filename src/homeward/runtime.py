"""Files the UI API and the background daemon share below the package's var folder.

- ``config.toml``: accounts and sync pairs (the UI writes it, the daemon reads it)
- ``requests/<pair>.json``: "run now" requests from the UI, consumed by the daemon
- ``status.json``: what the daemon is doing right now, written by the daemon
- ``homeward.log``: the daemon's log, shown in the UI
"""

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_VAR_DIR = Path("/var/packages/homeward/var")


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
    def status_file(self) -> Path:
        return self.var_dir / "status.json"

    @property
    def log_file(self) -> Path:
        return self.var_dir / "homeward.log"

    @property
    def backup_dir(self) -> Path:
        return self.var_dir / "config-backups"


@dataclass(frozen=True, slots=True)
class RunRequest:
    pair: str
    dry_run: bool = False
    allow_deletes: bool = False
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
                requested_at=float(data.get("requested_at", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        yield file, request


def read_status(paths: Paths) -> dict[str, Any]:
    """The daemon's status file, or an empty dict when there is none (yet)."""
    try:
        data = json.loads(paths.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
