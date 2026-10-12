"""Per-account session cookie storage with owner-only permissions.

Each account keeps pyicloud's trusted-session cookies in ``<state_dir>/sessions/<account>/``.
After one interactive login with 2FA (``homeward login``), syncs reuse that session
without a password until Apple expires it.
"""

import importlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homeward.core.photos import safe_component


class LoginRequired(Exception):
    """The stored session is missing or expired: a login with password (and 2FA) is needed."""

    def __init__(self, account: str, detail: str = "") -> None:
        message = f"account {account!r} needs a login: run `homeward login --account {account}`"
        super().__init__(f"{message} ({detail})" if detail else message)
        self.account = account


ServiceFactory = Callable[..., Any]


def pyicloud_service() -> ServiceFactory:
    """The real ``PyiCloudService`` class, imported only when an account is used."""
    module = importlib.import_module("pyicloud")
    factory: ServiceFactory = module.PyiCloudService
    return factory


def session_dir(state_dir: Path, account: str) -> Path:
    return state_dir / "sessions" / safe_component(account)


def ensure_session_dir(state_dir: Path, account: str) -> Path:
    path = session_dir(state_dir, account)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def open_service(
    account: str,
    apple_id: str,
    state_dir: Path,
    *,
    factory: ServiceFactory | None = None,
) -> Any:
    """Return a logged-in ``PyiCloudService`` from the stored trusted session.

    Never asks for a password or 2FA code; raises ``LoginRequired`` instead.
    """
    cookies = session_dir(state_dir, account)
    if not cookies.is_dir():
        raise LoginRequired(account, "no stored session")
    make = factory or pyicloud_service()
    try:
        # An empty password keeps pyicloud from asking the system keyring.
        service = make(apple_id, "", cookie_directory=str(cookies))
    except Exception as exc:
        if _is_login_error(exc):
            raise LoginRequired(account, str(exc)) from exc
        raise
    if service.requires_2fa or service.requires_2sa or not service.is_trusted_session:
        raise LoginRequired(account, "session is not trusted anymore")
    return service


def status(
    account: str, apple_id: str, state_dir: Path, *, factory: ServiceFactory | None = None
) -> str:
    """``"ok"`` if the stored session works, else ``"login_required"``."""
    try:
        open_service(account, apple_id, state_dir, factory=factory)
    except LoginRequired:
        return "login_required"
    return "ok"


def logout(account: str, state_dir: Path) -> None:
    """Forget the stored session of an account."""
    shutil.rmtree(session_dir(state_dir, account), ignore_errors=True)


def _is_login_error(exc: Exception) -> bool:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(
        names
        & {
            "PyiCloudFailedLoginException",
            "PyiCloud2FARequiredException",
            "PyiCloud2SARequiredException",
            "PyiCloudAuthRequiredException",
            "PyiCloudNoStoredPasswordAvailableException",
        }
    )
