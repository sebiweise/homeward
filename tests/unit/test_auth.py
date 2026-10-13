"""Apple ID login and stored sessions, with a stand-in for PyiCloudService."""

import stat
from pathlib import Path
from typing import Any

import pytest

from homeward.auth.session import (
    LoginRequired,
    logout,
    open_service,
    session_dir,
    session_rejected,
    status,
)
from homeward.auth.twofa import LoginError, LoginFlow


class PyiCloudFailedLoginException(Exception):
    pass


class FakeService:
    """Mimics PyiCloudService: a trusted session survives in the cookie directory."""

    good_password = "secret"
    code = "123456"

    def __init__(self, apple_id: str, password: str, cookie_directory: str) -> None:
        self.cookies = Path(cookie_directory) / "trusted"
        self.apple_id = apple_id
        self.calls: list[str] = []
        if self.cookies.exists():
            self.requires_2fa = False
        elif password == self.good_password:
            self.requires_2fa = True
        else:
            raise PyiCloudFailedLoginException("Invalid email/password combination.")
        self.requires_2sa = self.requires_2fa

    @property
    def is_trusted_session(self) -> bool:
        return self.cookies.exists()

    def validate_2fa_code(self, code: str) -> bool:
        if code != self.code:
            return False
        self.requires_2fa = self.requires_2sa = False
        return True

    def trust_session(self) -> bool:
        self.cookies.write_text("ok")
        return True


def test_no_session_needs_login(tmp_path: Path) -> None:
    with pytest.raises(LoginRequired, match="homeward login --account seb"):
        open_service("seb", "seb@example.com", tmp_path, factory=FakeService)
    assert status("seb", "seb@example.com", tmp_path, factory=FakeService) == "login_required"


def test_login_with_2fa_then_reuse_session(tmp_path: Path) -> None:
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    assert flow.login("secret") is True
    with pytest.raises(LoginError):
        flow.verify_2fa("000000")
    flow.verify_2fa(" 123456 ")

    mode = stat.S_IMODE(session_dir(tmp_path, "seb").stat().st_mode)
    assert mode == 0o700
    service: Any = open_service("seb", "seb@example.com", tmp_path, factory=FakeService)
    assert service.apple_id == "seb@example.com"
    assert status("seb", "seb@example.com", tmp_path, factory=FakeService) == "ok"

    logout("seb", tmp_path)
    assert status("seb", "seb@example.com", tmp_path, factory=FakeService) == "login_required"


def test_wrong_password(tmp_path: Path) -> None:
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    with pytest.raises(LoginError, match="Invalid"):
        flow.login("nope")


def test_expired_session_is_login_required(tmp_path: Path) -> None:
    session_dir(tmp_path, "seb").mkdir(parents=True)  # cookies exist, but not trusted
    with pytest.raises(LoginRequired, match="Invalid"):
        open_service("seb", "seb@example.com", tmp_path, factory=FakeService)


def test_verify_before_login(tmp_path: Path) -> None:
    flow = LoginFlow("seb", "x", tmp_path, factory=FakeService)
    with pytest.raises(LoginError):
        flow.verify_2fa("1")


def test_accounts_have_separate_sessions(tmp_path: Path) -> None:
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    flow.login("secret")
    flow.verify_2fa("123456")
    assert status("anna", "anna@example.com", tmp_path, factory=FakeService) == "login_required"
    assert session_dir(tmp_path, "../x").parent == tmp_path / "sessions"


def _sign_in(tmp_path: Path) -> None:
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    flow.login("secret")
    flow.verify_2fa("123456")


def test_sign_in_again_ignores_the_stored_session(tmp_path: Path) -> None:
    _sign_in(tmp_path)
    old = session_dir(tmp_path, "seb") / "trusted"
    old.write_text("broken")

    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    assert flow.login("secret") is True  # fresh cookies: Apple asks for a code again
    assert old.read_text() == "broken"  # syncs keep the old session meanwhile
    flow.verify_2fa("123456")

    assert old.read_text() == "ok"
    assert not (tmp_path / "sessions-pending" / "seb").exists()
    assert status("seb", "seb@example.com", tmp_path, factory=FakeService) == "ok"


def test_failed_sign_in_again_keeps_the_stored_session(tmp_path: Path) -> None:
    _sign_in(tmp_path)
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    with pytest.raises(LoginError):
        flow.login("nope")
    flow = LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService)
    flow.login("secret")
    with pytest.raises(LoginError):
        flow.verify_2fa("000000")
    assert status("seb", "seb@example.com", tmp_path, factory=FakeService) == "ok"


def test_logout_drops_a_pending_sign_in(tmp_path: Path) -> None:
    LoginFlow("seb", "seb@example.com", tmp_path, factory=FakeService).login("secret")
    logout("seb", tmp_path)
    assert not (tmp_path / "sessions-pending" / "seb").exists()


def test_session_rejected_by_apple() -> None:
    class Response(Exception):
        code = 421

    try:
        try:
            raise Response('Authentication required for Account. (421): {"reason":"x"}')
        except Response as inner:
            raise RuntimeError("Photos service not available") from inner
    except RuntimeError as exc:
        assert session_rejected(exc)
    assert session_rejected(RuntimeError('{"reason":"Invalid global session","error":2}'))
    assert session_rejected(PyiCloudFailedLoginException("nope"))
    assert not session_rejected(RuntimeError("Photos service not available"))
