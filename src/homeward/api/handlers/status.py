"""Overview for the status page: version, daemon state, each pair's last run and
notices, cancelling a queued or running sync and clearing the notices."""

import json
import os
import pwd
import time
from dataclasses import asdict
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import check_name, existing_state, package_version
from homeward.api.server import Context, Request
from homeward.core.photo_state import gone_counts
from homeward.runtime import (
    CancelRequest,
    Event,
    Level,
    drop_request,
    pending_requests,
    read_events,
    read_progress,
    read_status,
    record_event,
    request_cancel,
    write_json_atomic,
)

NOTICE_HOURS = 24
MAX_NOTICES = 10


def get(ctx: Context, _request: Request) -> dict[str, Any]:
    config = ctx.store.load()
    pending = {r.pair for r in pending_requests(ctx.paths)}
    daemon = read_status(ctx.paths)
    jobs: dict[str, Any] = daemon.get("jobs") or {}
    if not isinstance(jobs, dict):
        jobs = {}
    pairs = []
    for pair in config.pairs:
        with existing_state(config, pair.account) as state:
            runs = state.runs(pair.name, limit=1) if state else []
        pairs.append(
            {
                "name": pair.name,
                "account": pair.account,
                "mode": pair.mode.value,
                "last_run": asdict(runs[0]) if runs else None,
                "job": jobs.get(f"pair:{pair.name}"),
                "pending": pair.name in pending,
                "progress": _progress(ctx, daemon, pair.name, f"pair:{pair.name}"),
            }
        )
    gone: dict[str, dict[str, int]] = {}
    for account in {job.account for job in config.photos}:
        gone[account] = gone_counts(config.photo_state_path(account))
    photos = [
        {
            "name": job.name,
            "account": job.account,
            "gone": gone[job.account].get(job.name, 0),  # deleted in iCloud, kept on the NAS
            "job": jobs.get(f"photos:{job.name}"),
            "pending": f"photos:{job.name}" in pending,
            "progress": _progress(ctx, daemon, f"photos:{job.name}", f"photos:{job.name}"),
        }
        for job in config.photos
    ]
    return {
        "version": package_version(),
        "api_user": _user(),
        "daemon": daemon,
        "interval_minutes": config.interval_minutes,
        "pairs": pairs,
        "photos": photos,
        "notices": notices(ctx),
    }


def running_jobs(daemon: dict[str, Any]) -> set[str]:
    """The jobs the daemon runs now: pair names and ``photos:<job>``."""
    jobs = daemon.get("running_jobs")
    if isinstance(jobs, list):
        return {str(job) for job in jobs}
    running = daemon.get("running")  # a daemon from before parallel runs
    return {running} if isinstance(running, str) else set()


def _progress(ctx: Context, daemon: dict[str, Any], running: str, source: str) -> Any:
    """How far the job is, while the daemon runs it (``running`` as in ``running_jobs``)."""
    if running not in running_jobs(daemon):
        return None
    return read_progress(ctx.paths, source)


def clear_notices(ctx: Context, request: Request) -> dict[str, Any]:
    """Hide the current notices, of one ``source`` or all; the event log keeps them."""
    source = request.get("source", "")
    if source and source != "service" and not _is_job(source):
        raise ApiError("invalid_param", "source")
    cleared_at = time.time()
    data = _cleared(ctx)
    if source:
        data.setdefault("sources", {})[source] = cleared_at
    else:
        data = {"cleared_at": cleared_at}
    write_json_atomic(ctx.paths.notices_file, data)
    record_event(Level.INFO, source or "service", "notices_cleared",
                 "notices cleared in the DSM UI", paths=ctx.paths)  # fmt: skip
    return {"cleared_at": cleared_at}


def cancel(ctx: Context, request: Request) -> dict[str, Any]:
    """Stop a sync: a queued one is dropped, a running one is asked to stop.

    ``name`` is a pair name or ``photos:<job>``, as in ``running_jobs``. The daemon
    stops the run cleanly; files already synced stay, nothing is left half-written.
    """
    name = request.get("name")
    check_name(name.removeprefix("photos:"), "job")  # it becomes a file name
    if drop_request(ctx.paths, name):
        source = name if ":" in name else f"pair:{name}"
        record_event(Level.WARNING, source, "sync_cancelled",
                     "cancelled by the user before it started", paths=ctx.paths)  # fmt: skip
        return {"name": name, "cancelled": "queued"}
    if name not in running_jobs(read_status(ctx.paths)):
        raise ApiError("not_running", name, status=409)
    request_cancel(ctx.paths, CancelRequest(name))
    return {"name": name, "cancelled": "running"}


def notices(ctx: Context) -> list[dict[str, Any]]:
    """Running waits and recent warnings or errors, the newest per source and code.

    Left out: events from before the user last cleared the notices, events of
    pairs and photo backups that no longer exist, and events from
    before the last run of the same job that finished fine. The event log
    keeps all of them.
    """
    now = time.time()
    cleared = _cleared(ctx)
    since = max(now - NOTICE_HOURS * 3600, float(cleared.get("cleared_at", 0.0)))
    by_source: dict[str, float] = cleared.get("sources", {})
    shown = _ShownSources(_existing_sources(ctx))
    seen: set[tuple[str, str]] = set()
    result = []
    for event in read_events(ctx.paths, limit=2000):
        if event.time <= since:
            break
        if event.time <= by_source.get(event.source, 0.0) or not shown.keeps(event):
            continue
        waiting = event.until is not None and event.until > now
        key = (event.source, event.code)
        if key in seen:
            continue
        seen.add(key)  # only the newest event per source and code counts
        if not waiting and event.level == Level.INFO:
            continue
        result.append(asdict(event) | {"waiting": waiting})
        if len(result) >= MAX_NOTICES:
            break
    return result


class _ShownSources:
    """Decides, newest event first, which events still need the user's attention."""

    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.finished: set[str] = set()  # a later run of this source finished fine
        self.settled: set[str] = set()  # older events of this source are dealt with

    def keeps(self, event: Event) -> bool:
        source = event.source
        if source in self.settled or (_is_job(source) and source not in self.existing):
            return False
        if event.code == "sync_finished":
            self.finished.add(source)
        elif event.code == "sync_started" and source in self.finished:
            self.settled.add(source)  # everything before this good run is resolved
        return True


def _is_job(source: str) -> bool:
    return source.startswith(("pair:", "photos:"))


def _existing_sources(ctx: Context) -> set[str]:
    config = ctx.store.load()
    return {f"pair:{pair.name}" for pair in config.pairs} | {
        f"photos:{job.name}" for job in config.photos
    }


def _cleared(ctx: Context) -> dict[str, Any]:
    """``cleared_at`` (all notices) and ``sources`` (one job's) from the last clearing."""
    try:
        data = json.loads(ctx.paths.notices_file.read_text(encoding="utf-8"))
        cleared_at = float(data.get("cleared_at", 0.0))
        sources = {str(k): float(v) for k, v in dict(data.get("sources") or {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return {"cleared_at": cleared_at, "sources": sources}


def _user() -> str:
    """The system user the API runs as (it must be the package user, like the daemon)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())
