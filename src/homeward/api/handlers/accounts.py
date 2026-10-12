"""Apple ID accounts: list, add (with two-factor dialog), sign in again, remove.

Accounts live in the config as ``[[account]]`` tables (``name``, ``apple_id``).
Passwords are handed to the sign-in backend once and never written anywhere.
"""

import re
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import check_name
from homeward.api.server import Context, Request
from homeward.api.store import RawConfig

_APPLE_ID = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE = re.compile(r"^\d{6}$")


def list_accounts(ctx: Context, request: Request) -> dict[str, Any]:
    raw = ctx.store.read()
    config = ctx.store.parse(raw)
    result = []
    for account in _raw_accounts(raw):
        name = account["name"]
        result.append(
            {
                "name": name,
                "apple_id": account.get("apple_id", ""),
                "state": ctx.accounts.state(name).value,
                "pairs": [pair.name for pair in config.pairs if pair.account == name],
                "photos": [
                    job["name"]
                    for job in raw.get("photos", [])
                    if isinstance(job, dict) and job.get("account") == name
                ],
            }
        )
    return {"accounts": result}


def add(ctx: Context, request: Request) -> dict[str, Any]:
    name = check_name(request.get("name"), "account")
    apple_id = request.get("apple_id").strip()
    if not _APPLE_ID.match(apple_id):
        raise ApiError("invalid_param", "apple_id")
    if any(a["name"] == name for a in _raw_accounts(ctx.store.read())):
        raise ApiError("account_exists", name)
    result = ctx.accounts.login(name, apple_id, request.get("password"))

    def change(raw: RawConfig) -> None:
        accounts = raw.setdefault("account", [])
        if any(a.get("name") == name for a in accounts):
            raise ApiError("account_exists", name)
        accounts.append({"name": name, "apple_id": apple_id})

    try:
        ctx.store.update(change)
    except ApiError:
        ctx.accounts.logout(name)
        raise
    return {"name": name, "state": result.state.value}


def login(ctx: Context, request: Request) -> dict[str, Any]:
    """Sign in again, e.g. after Apple expired the session."""
    account = _find(ctx, request.get("name"))
    result = ctx.accounts.login(account["name"], account["apple_id"], request.get("password"))
    return {"name": account["name"], "state": result.state.value}


def verify(ctx: Context, request: Request) -> dict[str, Any]:
    account = _find(ctx, request.get("name"))
    code = request.get("code").replace(" ", "")
    if not _CODE.match(code):
        raise ApiError("invalid_code", "the code has six digits")
    result = ctx.accounts.verify_2fa(account["name"], code)
    return {"name": account["name"], "state": result.state.value}


def remove(ctx: Context, request: Request) -> dict[str, Any]:
    """Remove an account that no sync pair uses. Synced files stay untouched."""
    name = _find(ctx, request.get("name"))["name"]

    def change(raw: RawConfig) -> None:
        tables = [*raw.get("pair", []), *raw.get("photos", [])]
        users = [t.get("name") for t in tables if isinstance(t, dict) and t.get("account") == name]
        if users:
            raise ApiError("account_in_use", ", ".join(map(str, users)))
        raw["account"] = [a for a in _raw_accounts(raw) if a["name"] != name]

    ctx.store.update(change)
    ctx.accounts.logout(name)
    return {"name": name}


def _find(ctx: Context, name: str) -> dict[str, Any]:
    for account in _raw_accounts(ctx.store.read()):
        if account["name"] == name:
            return account
    raise ApiError("account_not_found", name, status=404)


def _raw_accounts(raw: RawConfig) -> list[dict[str, Any]]:
    accounts = raw.get("account", [])
    if not isinstance(accounts, list):
        raise ApiError("config_invalid", "'account' must be a list of [[account]] tables")
    return [a for a in accounts if isinstance(a, dict) and isinstance(a.get("name"), str)]
