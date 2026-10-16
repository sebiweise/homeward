import threading
import time
from pathlib import Path

import pytest

from homeward.api import login
from homeward.api.accounts import SessionState
from homeward.api.errors import ApiError
from homeward.api.login import Channel, PyiCloudBackend, run_helper
from homeward.runtime import Level, Paths, record_event


class FakeFlow:
    def __init__(self, password: str = "pw", code: str = "123456", needs_code: bool = True):
        self.password, self.code, self.needs_code = password, code, needs_code
        self.state_dir: Path | None = None
        self.account = ""
        self.error = "wrong password"

    def login(self, password: str) -> bool:
        if password != self.password:
            raise RuntimeError(self.error)
        return self.needs_code

    def verify_2fa(self, code: str) -> None:
        if code != self.code:
            raise RuntimeError("the 2FA code was rejected")
        assert self.state_dir is not None
        session = self.state_dir / "sessions" / self.account
        session.mkdir(parents=True, exist_ok=True)
        (session / "cookies").write_text("x")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login, "POLL_SECONDS", 0.01)


def _backend(tmp_path: Path, flow: FakeFlow) -> PyiCloudBackend:
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    paths.config.write_text('state_dir = "state"\n')

    def spawn(command: list[str], stdin: bytes) -> None:
        import json

        args = json.loads(stdin)
        flow.state_dir, flow.account = Path(args["state_dir"]), args["account"]
        channel = Channel(Path(command[-1]))

        def helper() -> None:
            (channel.dir / "pid").write_text(str(__import__("os").getpid()))
            try:
                run_helper(flow, args["password"], channel, timeout=5)
            finally:
                (channel.dir / "pid").unlink(missing_ok=True)

        threading.Thread(target=helper, daemon=True).start()

    return PyiCloudBackend(paths, spawn=spawn, answer_timeout=5)


def test_login_with_two_factor(tmp_path: Path) -> None:
    flow = FakeFlow()
    backend = _backend(tmp_path, flow)
    assert backend.state("me") == SessionState.SIGNED_OUT
    assert backend.login("me", "a@b.de", "pw").state == SessionState.NEEDS_2FA
    assert backend.state("me") == SessionState.NEEDS_2FA

    with pytest.raises(ApiError) as excinfo:
        backend.verify_2fa("me", "000000")
    assert excinfo.value.code == "code_rejected"
    assert backend.verify_2fa("me", "123456").state == SessionState.SIGNED_IN
    time.sleep(0.05)
    assert backend.state("me") == SessionState.SIGNED_IN

    # a sync that finds the session expired makes the account "signed out" again
    time.sleep(0.01)
    record_event(Level.ERROR, "photos:x", "login_required", paths=backend.paths, account="me")
    assert backend.state("me") == SessionState.SIGNED_OUT

    backend.logout("me")
    assert not (tmp_path / "state" / "sessions" / "me").exists()


def test_login_without_code_and_wrong_password(tmp_path: Path) -> None:
    backend = _backend(tmp_path, FakeFlow(needs_code=False))
    assert backend.login("me", "a@b.de", "pw").state == SessionState.SIGNED_IN
    with pytest.raises(ApiError) as excinfo:
        backend.login("me", "a@b.de", "wrong")
    assert excinfo.value.code == "login_failed"


def test_login_error_names_apple_reason(tmp_path: Path) -> None:
    flow = FakeFlow(needs_code=False)
    backend = _backend(tmp_path, flow)
    flow.error = "Invalid email/password combination. (-20101)"
    with pytest.raises(ApiError) as excinfo:
        backend.login("me", "a@b.de", "wrong")
    assert excinfo.value.code == "apple_wrong_password"


def test_code_without_waiting_sign_in(tmp_path: Path) -> None:
    backend = _backend(tmp_path, FakeFlow())
    with pytest.raises(ApiError) as excinfo:
        backend.verify_2fa("me", "123456")
    assert excinfo.value.code == "login_timeout"


def test_helper_gives_up(tmp_path: Path) -> None:
    channel = Channel(tmp_path / "c")
    channel.reset()
    run_helper(FakeFlow(), "pw", channel, timeout=0.05)
    assert channel.read("result") == {"seq": 1, "state": "expired"}


def test_only_the_login_helper_of_this_channel_counts(tmp_path: Path) -> None:
    proc, channel = tmp_path / "proc", tmp_path / "login" / "me"
    for pid, argv in (
        (10, [b"python3", b"-m", b"homeward.api.login", bytes(channel)]),
        (11, [b"python3", b"-m", b"homeward.api.login", bytes(tmp_path / "login" / "other")]),
        (12, [b"/usr/bin/synoscgi"]),  # a reused pid
    ):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(argv) + b"\0")

    assert login.is_helper_process(10, channel, proc)
    assert not login.is_helper_process(11, channel, proc)
    assert not login.is_helper_process(12, channel, proc)
    assert not login.is_helper_process(13, channel, proc)  # gone


def test_a_stale_pid_file_never_signals_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = Channel(tmp_path / "login" / "me")
    channel.dir.mkdir(parents=True)
    (channel.dir / "pid").write_text(str(__import__("os").getpid()))  # alive, not a helper
    killed: list[int] = []
    monkeypatch.setattr(login.os, "kill", lambda pid, sig: killed.append(sig))

    channel.stop_helper()

    assert killed == [0]  # only the liveness check, no SIGTERM
