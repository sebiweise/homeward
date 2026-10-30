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
from homeward.core.config import AccountConfig

_CODE = re.compile(r"^\d{6}$")


def list_accounts(ctx: Context, _request: Request) -> dict[str, Any]:
    config = ctx.store.load()
    return {
        "accounts": [
            {
                "name": account.name,
                "apple_id": account.apple_id,
                "state": ctx.accounts.state(account.name).value,
                "pairs": [pair.name for pair in config.pairs if pair.account == account.name],
                "photos": [job.name for job in config.photos if job.account == account.name],
            }
            for account in config.accounts
        ]
    }


def add(ctx: Context, request: Request) -> dict[str, Any]:
    name = check_name(request.get("name"), "account")
    apple_id = request.get("apple_id").strip()
    if not _is_apple_id(apple_id):
        raise ApiError("invalid_param", "apple_id")
    if ctx.store.load().account(name) is not None:
        raise ApiError("account_exists", name)
    result = ctx.accounts.login(name, apple_id, request.get("password"))

    def change(raw: RawConfig) -> None:
        accounts = raw.setdefault("account", [])
        if ctx.store.parse(raw).account(name) is not None:
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
    result = ctx.accounts.login(account.name, account.apple_id, request.get("password"))
    return {"name": account.name, "state": result.state.value}


def verify(ctx: Context, request: Request) -> dict[str, Any]:
    account = _find(ctx, request.get("name"))
    code = request.get("code").replace(" ", "")
    if not _CODE.match(code):
        raise ApiError("invalid_code", "the code has six digits")
    result = ctx.accounts.verify_2fa(account.name, code)
    return {"name": account.name, "state": result.state.value}


def remove(ctx: Context, request: Request) -> dict[str, Any]:
    """Remove an account that no sync pair uses. Synced files stay untouched."""
    name = _find(ctx, request.get("name")).name

    def change(raw: RawConfig) -> None:
        config = ctx.store.parse(raw)  # validated: every table below is a dict
        users = [p.name for p in config.pairs if p.account == name]
        users += [j.name for j in config.photos if j.account == name]
        if users:
            raise ApiError("account_in_use", ", ".join(users))
        raw["account"] = [a for a in raw.get("account", []) if a["name"] != name]

    ctx.store.update(change)
    ctx.accounts.logout(name)
    return {"name": name}


def _find(ctx: Context, name: str) -> AccountConfig:
    account = ctx.store.load().account(name)
    if account is None:
        raise ApiError("account_not_found", name, status=404)
    return account


def _is_apple_id(value: str) -> bool:
    """``name@domain.tld``: one ``@``, no whitespace, a dot inside the domain."""
    user, _, domain = value.partition("@")
    return (
        bool(user)
        and "@" not in domain
        and "." in domain[1:-1]
        and not any(char.isspace() for char in value)
    )
