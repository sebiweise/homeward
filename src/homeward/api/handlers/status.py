"""Overview for the status page: version, daemon state and each pair's last run."""

import os
import pwd
import time
from dataclasses import asdict
from typing import Any

from homeward.api.handlers.common import existing_state, package_version
from homeward.api.server import Context, Request
from homeward.runtime import Level, pending_requests, read_events, read_status

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
            }
        )
    photos = [
        {
            "name": job.name,
            "account": job.account,
            "job": jobs.get(f"photos:{job.name}"),
            "pending": f"photos:{job.name}" in pending,
        }
        for job in config.photos
    ]
    return {
        "version": package_version(),
        "api_user": _user(),
        "daemon": daemon,
        "pairs": pairs,
        "photos": photos,
        "notices": notices(ctx),
    }


def notices(ctx: Context) -> list[dict[str, Any]]:
    """Running waits and recent warnings or errors, the newest per source and code."""
    now = time.time()
    seen: set[tuple[str, str]] = set()
    result = []
    for event in read_events(ctx.paths, limit=2000):
        if event.time < now - NOTICE_HOURS * 3600:
            break
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


def _user() -> str:
    """The system user the API runs as (it must be the package user, like the daemon)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())
