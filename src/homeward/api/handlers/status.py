"""Overview for the status page: version, daemon state and each pair's last run."""

import time
from dataclasses import asdict
from typing import Any

from homeward.api.handlers.common import existing_state, package_version
from homeward.api.server import Context, Request
from homeward.runtime import Level, pending_requests, read_events, read_status

NOTICE_HOURS = 24
MAX_NOTICES = 10


def get(ctx: Context, request: Request) -> dict[str, Any]:
    config = ctx.store.load()
    pending = {r.pair for r in pending_requests(ctx.paths)}
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
                "pending": pair.name in pending,
            }
        )
    return {
        "version": package_version(),
        "daemon": read_status(ctx.paths),
        "pairs": pairs,
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
