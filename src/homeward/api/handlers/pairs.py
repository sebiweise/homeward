"""Sync pairs: list, create, update, delete and "run now".

A pair's name is its key in the state database, so it cannot be renamed. When a
pair's folders or account change, its sync state is reset: the next run is a
first run again, which never deletes or overwrites anything.
"""

import json
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import check_name
from homeward.api.server import Context, Request
from homeward.api.store import RawConfig
from homeward.core.config import REMOTE_PROVIDERS, PairConfig
from homeward.core.models import SyncMode
from homeward.core.planner import DeleteBrake
from homeward.core.state import StateDB
from homeward.runtime import RunRequest, request_run

# Changing one of these makes the stored sync state meaningless.
_IDENTITY = ("local", "remote", "account", "remote_provider")


def to_dict(pair: PairConfig) -> dict[str, Any]:
    return {
        "name": pair.name,
        "account": pair.account,
        "local": str(pair.local),
        "remote": pair.remote,
        "mode": pair.mode.value,
        "beta": pair.beta,
        "remote_provider": pair.remote_provider,
        "exclude": list(pair.exclude),
        "max_delete_ratio": pair.delete_brake.max_ratio,
        "max_delete_count": pair.delete_brake.max_count,
    }


def list_pairs(ctx: Context, _request: Request) -> dict[str, Any]:
    return {"pairs": [to_dict(pair) for pair in ctx.store.load().pairs]}


def create(ctx: Context, request: Request) -> dict[str, Any]:
    entry = _entry(ctx, request)

    def change(raw: RawConfig) -> None:
        pairs = raw.setdefault("pair", [])
        if any(p.get("name") == entry["name"] for p in pairs):
            raise ApiError("pair_exists", entry["name"])
        pairs.append(entry)

    config = ctx.store.update(change)
    return to_dict(config.pair(entry["name"]))


def update(ctx: Context, request: Request) -> dict[str, Any]:
    entry = _entry(ctx, request)
    old = _find(ctx, entry["name"])

    def change(raw: RawConfig) -> None:
        pairs = raw.get("pair", [])
        index = next((i for i, p in enumerate(pairs) if p.get("name") == entry["name"]), None)
        if index is None:
            raise ApiError("pair_not_found", entry["name"])
        pairs[index] = {**pairs[index], **entry}

    config = ctx.store.update(change)
    new = config.pair(entry["name"])
    old_identity = to_dict(old)
    new_identity = to_dict(new)
    if any(old_identity[key] != new_identity[key] for key in _IDENTITY):
        _reset_state(ctx, old)
        _reset_state(ctx, new)
    return to_dict(new)


def delete(ctx: Context, request: Request) -> dict[str, Any]:
    """Remove a pair from the config. Files on both sides stay untouched."""
    name = request.get("name")
    pair = _find(ctx, name)

    def change(raw: RawConfig) -> None:
        raw["pair"] = [p for p in raw.get("pair", []) if p.get("name") != name]

    ctx.store.update(change)
    _reset_state(ctx, pair)
    return {"name": name}


def run(ctx: Context, request: Request) -> dict[str, Any]:
    pair = _find(ctx, request.get("name"))
    request_run(
        ctx.paths,
        RunRequest(
            pair=pair.name,
            dry_run=request.flag("dry_run"),
            allow_deletes=request.flag("allow_deletes"),
        ),
    )
    return {"name": pair.name, "queued": True}


def _find(ctx: Context, name: str) -> PairConfig:
    for pair in ctx.store.load().pairs:
        if pair.name == name:
            return pair
    raise ApiError("pair_not_found", name, status=404)


def _reset_state(ctx: Context, pair: PairConfig) -> None:
    path = ctx.store.load().state_path(pair.account)
    if path.exists():
        with StateDB(path) as state:
            state.reset(pair.name)


def _entry(ctx: Context, request: Request) -> dict[str, Any]:
    """Validate the ``pair`` parameter (a JSON object) into a config table."""
    try:
        data = json.loads(request.get("pair"))
    except ValueError:
        raise ApiError("invalid_param", "pair must be a JSON object") from None
    if not isinstance(data, dict):
        raise ApiError("invalid_param", "pair must be a JSON object")

    def text(key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ApiError("missing_param", key)
        return value.strip()

    mode = text("mode")
    if mode not in {m.value for m in SyncMode}:
        raise ApiError("invalid_param", "mode")
    provider = data.get("remote_provider", "icloud_drive")
    if provider not in REMOTE_PROVIDERS:
        raise ApiError("invalid_param", "remote_provider")
    local = Path(text("local"))
    if not local.is_absolute() or ".." in local.parts:
        raise ApiError("invalid_param", "local")
    if not local.is_dir():
        raise ApiError("local_missing", str(local))
    account = check_name(text("account"), "account")
    accounts = _accounts(ctx)
    if provider == "icloud_drive" and account not in accounts:
        raise ApiError("account_not_found", account)
    exclude = _exclude(data)
    ratio, count = _delete_brake(data)
    return {
        "name": check_name(text("name"), "pair"),
        "account": account,
        **({"apple_id": accounts[account]} if account in accounts else {}),
        "local": str(local),
        "remote": text("remote"),
        "mode": mode,
        # Choosing "Both directions (beta)" in the UI is the opt-in the config asks for.
        "beta": mode == SyncMode.BIDIRECTIONAL,
        "remote_provider": provider,
        "exclude": exclude,
        "max_delete_ratio": ratio,
        "max_delete_count": count,
    }


def _exclude(data: dict[str, Any]) -> list[str]:
    exclude = data.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
        raise ApiError("invalid_param", "exclude")
    return [p.strip() for p in exclude if p.strip()]


def _delete_brake(data: dict[str, Any]) -> tuple[float, int]:
    defaults = DeleteBrake()
    ratio = data.get("max_delete_ratio", defaults.max_ratio)
    count = data.get("max_delete_count", defaults.max_count)
    if not isinstance(ratio, int | float) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
        raise ApiError("invalid_param", "max_delete_ratio")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ApiError("invalid_param", "max_delete_count")
    return float(ratio), count


def _accounts(ctx: Context) -> dict[str, str]:
    """Account name -> Apple ID."""
    accounts = ctx.store.read().get("account", [])
    return {
        a["name"]: str(a.get("apple_id", ""))
        for a in accounts
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    }
