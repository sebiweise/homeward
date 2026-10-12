"""Overview for the status page: version, daemon state and each pair's last run."""

from dataclasses import asdict
from typing import Any

from homeward.api.handlers.common import existing_state, package_version
from homeward.api.server import Context, Request
from homeward.runtime import pending_requests, read_status


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
    return {"version": package_version(), "daemon": read_status(ctx.paths), "pairs": pairs}
