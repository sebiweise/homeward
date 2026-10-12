import io
import json
import stat
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from homeward.api import toml_writer
from homeward.api.accounts import LoginResult, SessionState, UnavailableBackend
from homeward.api.auth import DsmAuthenticator
from homeward.api.cgi import handle, write_response
from homeward.api.errors import ApiError
from homeward.api.store import KEEP_BACKUPS, ConfigStore
from homeward.core.models import FileMeta
from homeward.core.state import StateDB
from homeward.runtime import (
    Level,
    Paths,
    pending_requests,
    read_events,
    record_event,
    take_requests,
)


class FakeAuth:
    def __init__(self, user: str = "admin") -> None:
        self.user = user

    def authenticate(self, environ: Mapping[str, str]) -> str:
        if environ.get("HTTP_X_SYNO_TOKEN") != "token":
            raise ApiError("not_authenticated", status=403)
        return self.user


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionState] = {}
        self.passwords: list[str] = []

    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        if password == "wrong":
            raise ApiError("login_failed")
        self.passwords.append(password)
        self.sessions[account] = SessionState.NEEDS_2FA
        return LoginResult(SessionState.NEEDS_2FA)

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        if code != "123456":
            raise ApiError("invalid_code")
        self.sessions[account] = SessionState.SIGNED_IN
        return LoginResult(SessionState.SIGNED_IN)

    def state(self, account: str) -> SessionState:
        return self.sessions.get(account, SessionState.SIGNED_OUT)

    def logout(self, account: str) -> None:
        self.sessions.pop(account, None)


class Client:
    def __init__(self, tmp_path: Path) -> None:
        self.paths = Paths(var_dir=tmp_path / "var", config=tmp_path / "var" / "config.toml")
        self.backend = FakeBackend()
        self.auth = FakeAuth()

    def call(self, method: str, post: bool = False, token: str = "token", **params: Any) -> Any:
        status, body = self.raw(method, post, token, **params)
        if not body["success"]:
            raise ApiError(body["error"]["code"], body["error"]["message"], status)
        return body["data"]

    def raw(self, method: str, post: bool, token: str, **params: Any) -> tuple[int, Any]:
        values = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in params.items()}
        environ = {"HTTP_X_SYNO_TOKEN": token}
        data = b""
        if post:
            data = urlencode({"method": method, **values}).encode()
            environ |= {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": "application/x-www-form-urlencoded; charset=UTF-8",
                "CONTENT_LENGTH": str(len(data)),
            }
        else:
            query = urlencode({"method": method, **values})
            environ |= {"REQUEST_METHOD": "GET", "QUERY_STRING": query}
        return handle(environ, io.BytesIO(data), self.auth, self.backend, self.paths)


@pytest.fixture
def client(tmp_path: Path) -> Client:
    return Client(tmp_path)


def _pair(tmp_path: Path, name: str = "docs", **overrides: Any) -> dict[str, Any]:
    local = tmp_path / "nas" / name
    local.mkdir(parents=True, exist_ok=True)
    pair = {
        "name": name,
        "account": "me",
        "local": str(local),
        "remote": f"{tmp_path}/cloud",
        "mode": "bidirectional",
        "remote_provider": "folder",
        "exclude": ["*.tmp", " "],
    }
    return pair | overrides


def _error(excinfo: pytest.ExceptionInfo[ApiError]) -> str:
    return excinfo.value.code


# --- request handling --------------------------------------------------------------


def test_requests_need_a_dsm_session(client: Client) -> None:
    status, body = client.raw("status.get", False, token="")
    assert status == 403
    assert body == {"success": False, "error": {"code": "not_authenticated", "message": ""}}


def test_unknown_method(client: Client) -> None:
    with pytest.raises(ApiError) as excinfo:
        client.call("nope.nothing")
    assert _error(excinfo) == "unknown_method"


def test_changes_need_post(client: Client, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.create", pair=_pair(tmp_path))
    assert _error(excinfo) == "post_required"
    assert not client.paths.config.exists()


def test_post_body_must_be_a_form(client: Client) -> None:
    environ = {"HTTP_X_SYNO_TOKEN": "token", "REQUEST_METHOD": "POST",
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "2"}  # fmt: skip
    status, body = handle(environ, io.BytesIO(b"{}"), client.auth, client.backend, client.paths)
    assert status == 400 and body["error"]["code"] == "unsupported_body"


def test_internal_errors_are_not_leaked(client: Client) -> None:
    client.paths.config.parent.mkdir(parents=True)
    client.paths.config.write_text('state_dir = "state"\n[[pair]]\nname = 1\n')
    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.list")
    assert _error(excinfo) == "config_invalid"


def test_write_response_is_cgi() -> None:
    out = io.StringIO()
    write_response(403, {"success": False}, out)
    head, body = out.getvalue().split("\r\n\r\n")
    assert head.startswith("Status: 403 Forbidden\r\n")
    assert "application/json" in head
    assert json.loads(body) == {"success": False}


# --- pairs ---------------------------------------------------------------------------


def test_pair_lifecycle(client: Client, tmp_path: Path) -> None:
    assert client.call("pairs.list") == {"pairs": []}
    created = client.call("pairs.create", post=True, pair=_pair(tmp_path))
    assert created["exclude"] == ["*.tmp"]
    assert created["max_delete_count"] == 50
    assert [p["name"] for p in client.call("pairs.list")["pairs"]] == ["docs"]

    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.create", post=True, pair=_pair(tmp_path))
    assert _error(excinfo) == "pair_exists"

    updated = client.call("pairs.update", post=True, pair=_pair(tmp_path, mode="upload"))
    assert updated["mode"] == "upload"

    client.call("pairs.delete", post=True, name="docs")
    assert client.call("pairs.list") == {"pairs": []}
    assert (tmp_path / "nas" / "docs").is_dir()  # files are never touched


def test_pair_validation(client: Client, tmp_path: Path) -> None:
    cases = {
        "invalid_name": {**_pair(tmp_path), "name": "../x"},
        "invalid_param": _pair(tmp_path, mode="mirror"),
        "local_missing": _pair(tmp_path, local=str(tmp_path / "missing")),
        "account_not_found": _pair(tmp_path, remote_provider="icloud_drive"),
        "missing_param": _pair(tmp_path, remote=""),
    }
    for code, pair in cases.items():
        with pytest.raises(ApiError) as excinfo:
            client.call("pairs.create", post=True, pair=pair)
        assert _error(excinfo) == code, pair
    assert not client.paths.config.exists()


def test_pair_interval(client: Client, tmp_path: Path) -> None:
    created = client.call("pairs.create", post=True, pair=_pair(tmp_path, interval_minutes=15))
    assert created["interval_minutes"] == 15
    assert tomllib.loads(client.paths.config.read_text())["pair"][0]["interval_minutes"] == 15

    updated = client.call("pairs.update", post=True, pair=_pair(tmp_path, interval_minutes=None))
    assert updated["interval_minutes"] is None  # back to the global schedule
    assert "interval_minutes" not in tomllib.loads(client.paths.config.read_text())["pair"][0]

    for bad in (4, "x", True, 10081):
        with pytest.raises(ApiError) as excinfo:
            client.call("pairs.update", post=True, pair=_pair(tmp_path, interval_minutes=bad))
        assert _error(excinfo) == "invalid_interval", bad


def test_global_schedule(client: Client, tmp_path: Path) -> None:
    settings = client.call("settings.get")
    assert settings == {
        "interval_minutes": 60,
        "default_interval_minutes": 60,
        "min_interval_minutes": 5,
        "max_interval_minutes": 10080,
    }
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    assert (
        client.call("settings.update", post=True, interval_minutes="15")["interval_minutes"] == 15
    )
    raw = tomllib.loads(client.paths.config.read_text())
    assert raw["daemon"] == {"interval_minutes": 15}
    assert [p["name"] for p in raw["pair"]] == ["docs"]  # the rest of the config stays
    assert client.call("settings.get")["interval_minutes"] == 15
    assert client.call("status.get")["interval_minutes"] == 15

    for bad in ("4", "abc", "1.5", "99999", ""):
        with pytest.raises(ApiError) as excinfo:
            client.call("settings.update", post=True, interval_minutes=bad)
        assert _error(excinfo) in ("invalid_interval", "missing_param"), bad
    with pytest.raises(ApiError) as excinfo:
        client.call("settings.update", interval_minutes="15")
    assert _error(excinfo) == "post_required"
    assert client.call("settings.get")["interval_minutes"] == 15


def test_changing_a_pairs_folder_resets_its_state(client: Client, tmp_path: Path) -> None:
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    state_path = client.paths.var_dir / "state" / "me.sqlite3"
    meta = FileMeta("a.txt", 1, 0.0, "v1")
    with StateDB(state_path) as state:
        state.record("docs", meta, meta)

    client.call("pairs.update", post=True, pair=_pair(tmp_path, mode="upload"))
    with StateDB(state_path) as state:
        assert "a.txt" in state.records("docs")

    other = tmp_path / "elsewhere"
    other.mkdir()
    client.call("pairs.update", post=True, pair=_pair(tmp_path, local=str(other)))
    with StateDB(state_path) as state:
        assert state.records("docs") == {}


def test_run_now_queues_a_request(client: Client, tmp_path: Path) -> None:
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    client.call("pairs.run", post=True, name="docs", allow_deletes="true")
    status = client.call("status.get")
    assert status["pairs"][0]["pending"] is True
    assert status["pairs"][0]["last_run"] is None

    (request,) = take_requests(client.paths)
    assert request.pair == "docs" and request.allow_deletes and not request.dry_run
    assert pending_requests(client.paths) == []

    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.run", post=True, name="other")
    assert _error(excinfo) == "pair_not_found"


# --- accounts ------------------------------------------------------------------------


def test_account_with_two_factor(client: Client, tmp_path: Path) -> None:
    added = client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    assert added == {"name": "me", "state": "needs_2fa"}
    assert "pw" not in client.paths.config.read_text()

    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.verify", post=True, name="me", code="12ab56")
    assert _error(excinfo) == "invalid_code"
    verified = client.call("accounts.verify", post=True, name="me", code="123 456")
    assert verified["state"] == "signed_in"

    client.call("pairs.create", post=True, pair=_pair(tmp_path, remote_provider="icloud_drive"))
    (account,) = client.call("accounts.list")["accounts"]
    assert account == {"name": "me", "apple_id": "a@b.de", "state": "signed_in",
                       "pairs": ["docs"], "photos": []}  # fmt: skip

    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.remove", post=True, name="me")
    assert _error(excinfo) == "account_in_use"
    client.call("pairs.delete", post=True, name="docs")
    client.call("accounts.remove", post=True, name="me")
    assert client.call("accounts.list") == {"accounts": []}
    assert client.backend.sessions == {}


def test_failed_login_saves_nothing(client: Client) -> None:
    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="wrong")
    assert _error(excinfo) == "login_failed"
    assert not client.paths.config.exists()


def test_account_validation(client: Client) -> None:
    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.add", post=True, name="me", apple_id="nope", password="pw")
    assert _error(excinfo) == "invalid_param"
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    assert _error(excinfo) == "account_exists"
    client.call("accounts.login", post=True, name="me", password="again")
    assert client.backend.passwords == ["pw", "again"]


def test_unavailable_backend(client: Client) -> None:
    client.backend = UnavailableBackend()  # type: ignore[assignment]
    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    assert _error(excinfo) == "icloud_unavailable"


# --- status and log ------------------------------------------------------------------


def test_status_shows_last_run_and_daemon(client: Client, tmp_path: Path) -> None:
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    with StateDB(client.paths.var_dir / "state" / "me.sqlite3") as state:
        state.log_run("docs", 1.0, dry_run=False, actions=3, errors=1, blocked=None)
        state.log_run("docs", 2.0, dry_run=True, actions=4, errors=0, blocked="too many")
    client.paths.status_file.write_text('{"running": "docs"}')

    status = client.call("status.get")
    assert status["daemon"] == {"running": "docs"}
    last = status["pairs"][0]["last_run"]
    assert last["started_at"] == 2.0 and last["blocked"] == "too many" and last["dry_run"]

    runs = client.call("log.runs", limit="1")["runs"]
    assert [run["actions"] for run in runs] == [4]


def test_log_tail(client: Client) -> None:
    assert client.call("log.tail") == {"lines": []}
    client.paths.var_dir.mkdir(parents=True)
    client.paths.log_file.write_text("".join(f"line {i}\n" for i in range(5000)))
    lines = client.call("log.tail", lines="3")["lines"]
    assert lines == ["line 4997", "line 4998", "line 4999"]
    assert len(client.call("log.tail", lines="99999")["lines"]) == 2000


# --- config store --------------------------------------------------------------------


def test_store_keeps_unknown_settings_and_backups(tmp_path: Path) -> None:
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    paths.config.write_text('state_dir = "state"\n\n[extra]\nlibrary = "/volume1/photo"\n')
    store = ConfigStore(paths)
    for i in range(KEEP_BACKUPS + 3):
        store.update(lambda raw, i=i: raw.__setitem__("counter", i))
    data = tomllib.loads(paths.config.read_text())
    assert data["extra"] == {"library": "/volume1/photo"}
    assert data["counter"] == KEEP_BACKUPS + 2
    assert len(list(paths.backup_dir.iterdir())) == KEEP_BACKUPS
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o640


def test_store_rejects_invalid_change(tmp_path: Path) -> None:
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    paths.config.write_text('state_dir = "state"\n')
    store = ConfigStore(paths)
    with pytest.raises(ApiError):
        store.update(lambda raw: raw.__setitem__("pair", [{"name": "x"}]))
    assert paths.config.read_text() == 'state_dir = "state"\n'


def test_toml_writer_round_trip() -> None:
    data = {
        "state_dir": "state",
        "flag": True,
        "ratio": 0.1,
        "weird key": 'quote " and \\ and \n and ü',
        "empty": [],
        "photos": {"library": "/v", "nested": {"x": 1}},
        "pair": [{"name": "a", "exclude": ["*.tmp", "x"]}, {"name": "b"}],
    }
    assert tomllib.loads(toml_writer.dumps(data)) == data


# --- DSM session check ---------------------------------------------------------------


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_dsm_authenticator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cgi = _script(tmp_path / "auth.cgi", '[ "$QUERY_STRING" = "SynoToken=tok" ] && echo admin')
    auth = DsmAuthenticator(cgi=cgi)
    monkeypatch.setattr(auth, "is_admin", lambda user: user == "admin")
    assert auth.authenticate({"HTTP_X_SYNO_TOKEN": "tok", "HTTP_COOKIE": "id=1"}) == "admin"

    for environ in ({}, {"HTTP_X_SYNO_TOKEN": "other"}):
        with pytest.raises(ApiError) as excinfo:
            auth.authenticate(environ)
        assert _error(excinfo) == "not_authenticated"

    monkeypatch.setattr(auth, "is_admin", lambda user: False)
    with pytest.raises(ApiError) as excinfo:
        auth.authenticate({"HTTP_X_SYNO_TOKEN": "tok"})
    assert _error(excinfo) == "not_admin"


def test_dsm_authenticator_missing_cgi(tmp_path: Path) -> None:
    auth = DsmAuthenticator(cgi=str(tmp_path / "missing"))
    with pytest.raises(ApiError) as excinfo:
        auth.authenticate({"HTTP_X_SYNO_TOKEN": "tok"})
    assert _error(excinfo) == "not_authenticated"
    assert not auth.is_admin("nobody-at-all")


# --- photos --------------------------------------------------------------------------


def _photos(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    target = tmp_path / "photo"
    target.mkdir(exist_ok=True)
    return {"name": "fotos", "account": "me", "target": str(target)} | overrides


def test_photos_lifecycle_with_all_options(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    defaults = client.call("photos.defaults")
    assert defaults["hidden"] is False and defaults["xmp"] is True

    created = client.call("photos.create", post=True, photos=_photos(tmp_path))
    assert created["libraries"] == ["personal", "shared", "shared_albums"]
    assert created["interval_minutes"] is None
    assert created["folder_scheme"] == "{year}/{month}"

    changed = _photos(
        tmp_path,
        hidden=True,
        videos=False,
        libraries=["shared", "personal"],
        folder_scheme="{year}/{month}/{day}",
        albums_folder="Meine Alben",
        interval_minutes=720,
    )
    updated = client.call("photos.update", post=True, photos=changed)
    assert updated["interval_minutes"] == 720
    assert updated["hidden"] is True and updated["videos"] is False
    assert updated["libraries"] == ["personal", "shared"]

    raw = tomllib.loads(client.paths.config.read_text())
    (job,) = raw["photos"]
    assert job["apple_id"] == "a@b.de" and job["albums_folder"] == "Meine Alben"
    assert job["interval_minutes"] == 720
    reset = client.call("photos.update", post=True, photos=changed | {"interval_minutes": None})
    assert reset["interval_minutes"] is None
    assert "interval_minutes" not in tomllib.loads(client.paths.config.read_text())["photos"][0]
    assert client.call("accounts.list")["accounts"][0]["photos"] == ["fotos"]

    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.remove", post=True, name="me")
    assert _error(excinfo) == "account_in_use"

    client.call("photos.delete", post=True, name="fotos")
    assert client.call("photos.list") == {"photos": []}
    assert (tmp_path / "photo").is_dir()


def test_photos_validation(client: Client, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        client.call("photos.create", post=True, photos=_photos(tmp_path))
    assert _error(excinfo) == "account_not_found"
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    cases = {
        "invalid_scheme": _photos(tmp_path, folder_scheme="{album}"),
        "invalid_param": _photos(tmp_path, libraries=[]),
        "local_missing": _photos(tmp_path, target=str(tmp_path / "missing")),
    }
    for code, job in cases.items():
        with pytest.raises(ApiError) as excinfo:
            client.call("photos.create", post=True, photos=job)
        assert _error(excinfo) == code, job
    for bad in ({"hidden": "yes"}, {"albums_folder": "a/b"}, {"folder_scheme": "../{year}"}):
        with pytest.raises(ApiError):
            client.call("photos.create", post=True, photos=_photos(tmp_path, **bad))
    with pytest.raises(ApiError) as excinfo:
        client.call("photos.update", post=True, photos=_photos(tmp_path))
    assert _error(excinfo) == "photos_not_found"


# --- events --------------------------------------------------------------------------


def test_events_and_notices(client: Client) -> None:
    paths = client.paths
    now = time.time()
    record_event(Level.INFO, "service", "sync_started", paths=paths)
    record_event(Level.WARNING, "photos:fotos", "icloud_indexing", "PyiCloud...", until=now + 300,
                 paths=paths, attempt=1)  # fmt: skip
    record_event(Level.ERROR, "pair:docs", "file_failed", "a.txt: 503", paths=paths)
    record_event(Level.ERROR, "pair:docs", "file_failed", "b.txt: 503", paths=paths)
    paths.events_file.open("a").write("not json\n")

    events = client.call("log.events")["events"]
    assert [e["code"] for e in events] == ["file_failed", "file_failed", "icloud_indexing",
                                           "sync_started"]  # fmt: skip
    assert events[2]["details"] == {"attempt": 1}
    errors = client.call("log.events", level="error")["events"]
    assert {e["level"] for e in errors} == {"error"}
    with pytest.raises(ApiError):
        client.call("log.events", level="debug")

    notices = client.call("status.get")["notices"]
    assert [(n["code"], n["waiting"]) for n in notices] == [("file_failed", False),
                                                            ("icloud_indexing", True)]  # fmt: skip
    assert notices[0]["message"] == "b.txt: 503"


def test_event_log_rotates_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("homeward.runtime.MAX_EVENTS_BYTES", 200)
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    for i in range(10):
        record_event(Level.INFO, "service", "retry", f"attempt {i}", paths=paths)
    assert paths.events_file.with_suffix(".jsonl.1").exists()
    assert [e.message for e in read_events(paths, limit=3)] == ["attempt 9", "attempt 8",
                                                                "attempt 7"]  # fmt: skip
    blocked = Paths(var_dir=paths.events_file, config=tmp_path / "x")  # a file, not a folder
    record_event(Level.ERROR, "service", "retry", paths=blocked)


def test_photos_run_and_status(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("photos.create", post=True, photos=_photos(tmp_path))
    client.paths.status_file.write_text(
        json.dumps({"running": None, "jobs": {"photos:fotos": {"result": "ok", "next_due": 5.0}}})
    )
    client.call("photos.run", post=True, name="fotos", dry_run="1")
    (photos,) = client.call("status.get")["photos"]
    assert photos["pending"] and photos["job"]["result"] == "ok"
    (request,) = take_requests(client.paths)
    assert request.pair == "photos:fotos" and request.dry_run
    with pytest.raises(ApiError):
        client.call("photos.run", post=True, name="other")


def test_pairs_get_the_apple_id_of_their_account(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("pairs.create", post=True, pair=_pair(tmp_path, remote_provider="icloud_drive"))
    (pair,) = tomllib.loads(client.paths.config.read_text())["pair"]
    assert pair["apple_id"] == "a@b.de"


def test_version_comes_from_the_dsm_package(tmp_path: Path) -> None:
    from homeward.api.handlers.common import package_version

    info = tmp_path / "INFO"
    info.write_text('package="homeward"\nversion="0.1.0-2"\n')
    assert package_version(info) == "0.1.0-2"
    assert package_version(tmp_path / "missing")  # falls back to the wheel's version
