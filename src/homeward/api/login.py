"""Apple ID sign-in from the DSM UI.

A sign-in with two-factor authentication spans two web requests (password, then
code), but the pyicloud session between them lives in memory. So the first request
starts a small helper process that signs in and then waits for the code; the API
talks to it through files in ``var/login/<account>/``:

- ``code.json``   ``{"seq": n, "code": "123456"}``, written by the API
- ``result.json`` ``{"seq": n, "state": ..., "error": ...}``, written by the helper
- ``pid``         the helper's process id

``seq`` 0 answers the password, every code gets the next number. The helper ends
after a successful sign-in, a failed password or ``HELPER_TIMEOUT`` seconds.
"""

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from homeward.api.accounts import AccountBackend, LoginResult, SessionState
from homeward.api.errors import ApiError
from homeward.api.store import ConfigStore
from homeward.auth.session import logout, session_dir
from homeward.auth.twofa import LoginFlow
from homeward.core.photos import safe_component
from homeward.runtime import Paths, read_events, write_json_atomic

HELPER_MODULE = "homeward.api.login"
HELPER_TIMEOUT = 600.0
ANSWER_TIMEOUT = 90.0
POLL_SECONDS = 0.2


class Flow(Protocol):
    def login(self, password: str) -> bool: ...
    def verify_2fa(self, code: str) -> None: ...


class Channel:
    """The files one sign-in uses to talk between API and helper."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def reset(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, mode=0o700)

    def write(self, name: str, data: dict[str, Any]) -> None:
        write_json_atomic(self.dir / f"{name}.json", data, mode=0o600)

    def read(self, name: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.dir / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def wait_for(self, name: str, seq: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.read(name)
            if data is not None and data.get("seq") == seq:
                return data
            time.sleep(POLL_SECONDS)
        return None

    @property
    def pid(self) -> int | None:
        try:
            return int((self.dir / "pid").read_text())
        except (OSError, ValueError):
            return None

    def helper_alive(self) -> bool:
        pid = self.pid
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def stop_helper(self) -> None:
        pid = self.pid
        # A stale pid file may name an unrelated process by now: only signal ours.
        if pid is not None and self.helper_alive() and is_helper_process(pid, self.dir):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


def is_helper_process(pid: int, channel_dir: Path, proc: Path = Path("/proc")) -> bool:
    """Whether ``pid`` runs ``homeward.api.login`` for this channel (Linux ``/proc``)."""
    try:
        argv = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return HELPER_MODULE.encode() in argv and os.fsencode(channel_dir) in argv


def run_helper(
    flow: Flow, password: str, channel: Channel, timeout: float = HELPER_TIMEOUT
) -> None:
    """Sign in, then answer codes until the sign-in succeeds or the time is up."""
    try:
        needs_code = flow.login(password)
    except Exception as exc:
        channel.write("result", {"seq": 0, "state": "failed", "error": str(exc)})
        return
    if not needs_code:
        channel.write("result", {"seq": 0, "state": SessionState.SIGNED_IN})
        return
    channel.write("result", {"seq": 0, "state": SessionState.NEEDS_2FA})
    deadline = time.monotonic() + timeout
    answered = 0
    while time.monotonic() < deadline:
        request = channel.read("code")
        if request is None or not isinstance(request.get("seq"), int) or request["seq"] <= answered:
            time.sleep(POLL_SECONDS)
            continue
        answered = request["seq"]
        try:
            flow.verify_2fa(str(request.get("code", "")))
        except Exception as exc:
            channel.write("result", {"seq": answered, "state": SessionState.NEEDS_2FA,
                                     "error": str(exc)})  # fmt: skip
            continue
        channel.write("result", {"seq": answered, "state": SessionState.SIGNED_IN})
        return
    channel.write("result", {"seq": answered + 1, "state": "expired"})


class PyiCloudBackend(AccountBackend):
    """:class:`~homeward.api.accounts.AccountBackend` that signs in with pyicloud."""

    def __init__(
        self,
        paths: Paths,
        *,
        spawn: Callable[[list[str], bytes], None] | None = None,
        answer_timeout: float = ANSWER_TIMEOUT,
    ) -> None:
        self.paths = paths
        self._spawn = spawn or _spawn_detached
        self.answer_timeout = answer_timeout

    def _channel(self, account: str) -> Channel:
        return Channel(self.paths.var_dir / "login" / safe_component(account))

    def _state_dir(self) -> Path:
        return ConfigStore(self.paths).load().state_dir

    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        channel = self._channel(account)
        channel.stop_helper()
        channel.reset()
        args = {
            "account": account,
            "apple_id": apple_id,
            "state_dir": str(self._state_dir()),
            "password": password,
        }
        command = [sys.executable, "-m", HELPER_MODULE, str(channel.dir)]
        self._spawn(command, json.dumps(args).encode())
        answer = channel.wait_for("result", 0, self.answer_timeout)
        if answer is None:
            channel.stop_helper()
            raise ApiError("login_timeout", "Apple did not answer in time")
        if answer["state"] == "failed":
            raise ApiError("login_failed", str(answer.get("error", "")))
        return LoginResult(SessionState(answer["state"]))

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        channel = self._channel(account)
        if not channel.helper_alive():
            raise ApiError("login_timeout", "the sign-in is no longer waiting for a code")
        last = channel.read("result") or {}
        seq = int(last.get("seq", 0)) + 1
        channel.write("code", {"seq": seq, "code": code})
        answer = channel.wait_for("result", seq, self.answer_timeout)
        if answer is None:
            raise ApiError("login_timeout", "Apple did not answer in time")
        if answer.get("error"):
            raise ApiError("code_rejected", str(answer["error"]))
        return LoginResult(SessionState(answer["state"]))

    def state(self, account: str) -> SessionState:
        channel = self._channel(account)
        last = channel.read("result") or {}
        if last.get("state") == SessionState.NEEDS_2FA and channel.helper_alive():
            return SessionState.NEEDS_2FA
        try:
            session = session_dir(self._state_dir(), account)
            signed_in_at = max(f.stat().st_mtime for f in session.iterdir())
        except (OSError, ValueError, ApiError):
            return SessionState.SIGNED_OUT
        for event in read_events(self.paths, limit=500):
            if event.time <= signed_in_at:
                break
            if event.code == "login_required" and event.details.get("account") == account:
                return SessionState.SIGNED_OUT
        return SessionState.SIGNED_IN

    def logout(self, account: str) -> None:
        self._channel(account).stop_helper()
        shutil.rmtree(self._channel(account).dir, ignore_errors=True)
        try:
            state_dir = self._state_dir()
        except ApiError:
            return
        logout(account, state_dir)


def pyicloud_available() -> bool:
    return importlib.util.find_spec("pyicloud") is not None


def _spawn_detached(command: list[str], stdin: bytes) -> None:
    """Start the helper so it outlives the CGI request; the password goes via stdin."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(stdin)
    proc.stdin.close()


def main(argv: list[str]) -> int:
    channel = Channel(Path(argv[0]))
    args = json.loads(sys.stdin.read())
    (channel.dir / "pid").write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    flow = LoginFlow(args["account"], args["apple_id"], Path(args["state_dir"]))
    try:
        run_helper(flow, args["password"], channel)
    finally:
        (channel.dir / "pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
