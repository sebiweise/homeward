"""Apple ID sign-in as the API sees it.

The API never imports pyicloud. A backend implementing :class:`AccountBackend`
does the actual sign-in, two-factor verification and session storage; the
password is passed through once and never stored by Homeward.
"""

import importlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from homeward.api.errors import ApiError


class SessionState(StrEnum):
    SIGNED_IN = "signed_in"
    NEEDS_2FA = "needs_2fa"
    SIGNED_OUT = "signed_out"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LoginResult:
    state: SessionState


class AccountBackend(Protocol):
    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        """Sign in; returns ``NEEDS_2FA`` when Apple asks for a code."""
        ...

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        """Finish a sign-in with the six-digit code from a trusted device."""
        ...

    def state(self, account: str) -> SessionState:
        """Whether the stored session is still valid, without contacting Apple if possible."""
        ...

    def logout(self, account: str) -> None:
        """Forget the stored session of an account."""
        ...


class UnavailableBackend:
    """Used until an iCloud provider with sign-in support is installed."""

    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        raise ApiError("icloud_unavailable", "iCloud sign-in is not available in this build")

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        raise ApiError("icloud_unavailable", "iCloud sign-in is not available in this build")

    def state(self, account: str) -> SessionState:
        return SessionState.UNAVAILABLE

    def logout(self, account: str) -> None:
        return None


def default_backend() -> AccountBackend:
    """The pyicloud-based backend when installed, else :class:`UnavailableBackend`."""
    try:
        module = importlib.import_module("homeward.auth.backend")
    except ImportError:
        return UnavailableBackend()
    backend: AccountBackend = module.PyiCloudBackend()
    return backend
