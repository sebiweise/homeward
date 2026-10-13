"""Two-factor authentication flow: password login, then the code from an Apple device."""

from pathlib import Path
from typing import Any

from homeward.auth.session import (
    ServiceFactory,
    commit_pending_session,
    fresh_pending_session_dir,
    pyicloud_service,
)


class LoginError(Exception):
    """Apple rejected the password or the 2FA code."""


class LoginFlow:
    """One interactive login of an account; the trusted session lands in its session dir.

    ``login`` returns True when Apple wants a 2FA code, which ``verify_2fa`` then takes.
    The sign-in starts without cookies and replaces the stored session only when it
    succeeds; until then syncs keep using the old one.
    """

    def __init__(
        self,
        account: str,
        apple_id: str,
        state_dir: Path,
        *,
        factory: ServiceFactory | None = None,
    ) -> None:
        self.account = account
        self.apple_id = apple_id
        self.state_dir = state_dir
        self._factory = factory
        self._service: Any = None

    def login(self, password: str) -> bool:
        cookies = fresh_pending_session_dir(self.state_dir, self.account)
        make = self._factory or pyicloud_service()
        try:
            self._service = make(self.apple_id, password, cookie_directory=str(cookies))
        except Exception as exc:
            raise LoginError(f"login failed: {exc}") from exc
        if self._service.requires_2sa and not self._service.requires_2fa:
            raise LoginError("legacy two-step verification is not supported, enable 2FA")
        if self._service.requires_2fa:
            return True
        commit_pending_session(self.state_dir, self.account)
        return False

    def verify_2fa(self, code: str) -> None:
        if self._service is None:
            raise LoginError("call login() first")
        if not self._service.validate_2fa_code(code.strip()):
            raise LoginError("the 2FA code was rejected")
        if not self._service.is_trusted_session:
            self._service.trust_session()
        commit_pending_session(self.state_dir, self.account)

    @property
    def service(self) -> Any:
        """The logged-in ``PyiCloudService``."""
        return self._service
