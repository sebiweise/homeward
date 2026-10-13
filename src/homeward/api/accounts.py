"""Apple ID sign-in as the API sees it.

The API never imports pyicloud. A backend implementing :class:`AccountBackend`
does the actual sign-in, two-factor verification and session storage; the
password is passed through once and never stored by Homeward.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from homeward.api.errors import ApiError

if TYPE_CHECKING:
    from homeward.runtime import Paths


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


class UnavailableBackend(AccountBackend):
    """Used until an iCloud provider with sign-in support is installed."""

    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        raise ApiError("icloud_unavailable", "iCloud sign-in is not available in this build")

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        raise ApiError("icloud_unavailable", "iCloud sign-in is not available in this build")

    def state(self, account: str) -> SessionState:
        return SessionState.UNAVAILABLE

    def logout(self, account: str) -> None:
        return None


def default_backend(paths: "Paths") -> AccountBackend:
    """The pyicloud sign-in when pyicloud is installed, else :class:`UnavailableBackend`."""
    from homeward.api.login import PyiCloudBackend, pyicloud_available

    if not pyicloud_available():
        return UnavailableBackend()
    return PyiCloudBackend(paths)
