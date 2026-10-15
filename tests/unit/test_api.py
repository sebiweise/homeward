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
from homeward.core.config import parse_config
from homeward.core.models import FileMeta
from homeward.core.planner import DeleteBrake
from homeward.core.state import StateDB
from homeward.runtime import (
    Level,
    Paths,
    pending_requests,
    read_events,
    record_event,
    take_requests,
    write_progress,
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
    pair = _pair(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.create", pair=pair)
    assert _error(excinfo) == "post_required"
    assert not client.paths.config.exists()


def test_post_body_must_be_a_form(client: Client) -> None:
    environ = {"HTTP_X_SYNO_TOKEN": "token", "REQUEST_METHOD": "POST",
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "2"}  # fmt: skip
    status, body = handle(environ, io.BytesIO(b"{}"), client.auth, client.backend, client.paths)
    assert status == 400
    assert body["error"]["code"] == "unsupported_body"


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
    assert created["beta"]  # choosing bidirectional in the UI opts into the beta
    assert [p["name"] for p in client.call("pairs.list")["pairs"]] == ["docs"]

    pair = _pair(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        client.call("pairs.create", post=True, pair=pair)
    assert _error(excinfo) == "pair_exists"

    updated = client.call("pairs.update", post=True, pair=_pair(tmp_path, mode="upload"))
    assert updated["mode"] == "upload"
    assert not updated["beta"]

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
    assert request.pair == "docs"
    assert request.allow_deletes
    assert not request.dry_run
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
    assert status["stale_days"] == 3
    assert (status["pairs"][0]["files"], status["pairs"][0]["bytes"]) == (0, 0)
    last = status["pairs"][0]["last_run"]
    assert last["started_at"] == 2.0
    assert last["blocked"] == "too many"
    assert last["dry_run"]

    runs = client.call("log.runs", limit="1")["runs"]
    assert [run["actions"] for run in runs] == [4]


def test_status_shows_the_progress_of_the_running_job(client: Client, tmp_path: Path) -> None:
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    write_progress(client.paths, "pair:docs", {"phase": "transferring", "done": 2, "total": 5})
    assert client.call("status.get")["pairs"][0]["progress"] is None  # not running

    client.paths.status_file.write_text('{"running": "docs"}')  # an older daemon
    progress = client.call("status.get")["pairs"][0]["progress"]
    assert (progress["phase"], progress["done"], progress["total"]) == ("transferring", 2, 5)

    # Runs of different Apple IDs side by side
    client.paths.status_file.write_text('{"running": "docs", "running_jobs": ["docs", "photos:x"]}')
    assert client.call("status.get")["pairs"][0]["progress"]["done"] == 2
    request_cancel_ok = client.call("status.cancel", post=True, name="photos:x")
    assert request_cancel_ok["cancelled"] == "running"


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
    assert defaults["hidden"] is False
    assert defaults["xmp"] is True
    assert defaults["source_folders"] is True
    assert defaults["interval_minutes"] == 30  # what the dialog offers for a new backup
    assert (defaults["max_delete_ratio"], defaults["max_delete_count"]) == (0.1, 50)
    assert (defaults["trash_days"], defaults["workers"], defaults["album_names"]) == (30, 4, [])

    created = client.call("photos.create", post=True, photos=_photos(tmp_path))
    assert created["libraries"] == ["personal", "shared", "shared_albums"]
    assert created["interval_minutes"] is None
    assert created["folder_scheme"] == "{year}/{month}"

    changed = _photos(
        tmp_path,
        hidden=True,
        videos=False,
        libraries=["shared", "personal"],
        source_folders=False,
        folder_scheme="{year}/{month}/{day}",
        albums_folder="Meine Alben",
        interval_minutes=720,
        max_delete_ratio=0.25,
        max_delete_count=100,
        trash_days=0,
        workers=2,
        album_names=[" Urlaub ", "", "Familie", "Urlaub"],
    )
    updated = client.call("photos.update", post=True, photos=changed)
    assert updated["album_names"] == ["Urlaub", "Familie"]
    assert (updated["max_delete_ratio"], updated["max_delete_count"]) == (0.25, 100)
    assert (updated["trash_days"], updated["workers"]) == (0, 2)
    assert updated["interval_minutes"] == 720
    assert updated["hidden"] is True
    assert updated["videos"] is False
    assert updated["libraries"] == ["personal", "shared"]
    assert updated["source_folders"] is False

    raw = tomllib.loads(client.paths.config.read_text())
    (job,) = raw["photos"]
    assert updated["interval_minutes"] == 720
    assert job["apple_id"] == "a@b.de"
    assert job["albums_folder"] == "Meine Alben"
    assert job["source_folders"] is False
    config = parse_config(raw, base=client.paths.config.parent).photos[0]
    assert config.album_names == ("Urlaub", "Familie")
    assert config.delete_brake == DeleteBrake(max_ratio=0.25, max_count=100)
    assert (config.trash_days, config.workers) == (0, 2)
    assert client.call("accounts.list")["accounts"][0]["photos"] == ["fotos"]

    with pytest.raises(ApiError) as excinfo:
        client.call("accounts.remove", post=True, name="me")
    assert _error(excinfo) == "account_in_use"

    client.call("photos.delete", post=True, name="fotos")
    assert client.call("photos.list") == {"photos": []}
    assert (tmp_path / "photo").is_dir()


def test_photos_validation(client: Client, tmp_path: Path) -> None:
    photos = _photos(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        client.call("photos.create", post=True, photos=photos)
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
    bad_values = (
        {"hidden": "yes"},
        {"source_folders": 0},
        {"albums_folder": "a/b"},
        {"folder_scheme": "../{year}"},
        {"max_delete_ratio": 1.5},
        {"max_delete_count": 2.5},
        {"trash_days": -1},
        {"workers": 9},
        {"workers": True},
        {"album_names": "Urlaub"},
    )
    for bad in bad_values:
        invalid = _photos(tmp_path, **bad)
        with pytest.raises(ApiError):
            client.call("photos.create", post=True, photos=invalid)
    photos = _photos(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        client.call("photos.update", post=True, photos=photos)
    assert _error(excinfo) == "photos_not_found"


# --- events --------------------------------------------------------------------------


def test_events_and_notices(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    client.call("photos.create", post=True, photos=_photos(tmp_path))
    paths = client.paths
    now = time.time()
    record_event(Level.INFO, "service", "sync_started", paths=paths)
    record_event(Level.WARNING, "photos:fotos", "icloud_indexing", "PyiCloud...", until=now + 300,
                 paths=paths, attempt=1)  # fmt: skip
    record_event(Level.ERROR, "pair:docs", "file_failed", "a.txt: 503", paths=paths)
    record_event(Level.ERROR, "pair:docs", "file_failed", "b.txt: 503", paths=paths)
    record_event(Level.INFO, "service", "sync_started", "Invalid global session", paths=paths)
    paths.events_file.open("a").write("not json\n")

    events = client.call("log.events")["events"]
    assert [e["code"] for e in events] == ["sync_started", "file_failed", "file_failed",
                                           "icloud_indexing", "sync_started"]  # fmt: skip
    assert events[3]["details"] == {"attempt": 1}
    # warnings and errors carry the code of a text explaining Apple's message; info never
    assert "hint" not in events[0] and events[1]["hint"] is None
    errors = client.call("log.events", level="error")["events"]
    assert {e["level"] for e in errors} == {"error"}
    with pytest.raises(ApiError):
        client.call("log.events", level="debug")

    notices = client.call("status.get")["notices"]
    assert [(n["code"], n["waiting"]) for n in notices] == [("file_failed", False),
                                                            ("icloud_indexing", True)]  # fmt: skip
    assert notices[0]["message"] == "b.txt: 503"


def _event(paths: Paths, age: float, source: str, code: str,
           level: Level = Level.WARNING) -> None:  # fmt: skip
    """Write an event ``age`` seconds in the past."""
    event = {"time": time.time() - age, "level": level.value, "source": source, "code": code}
    with paths.events_file.open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def test_notices_leave_out_deleted_jobs_and_resolved_events(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    client.call("photos.create", post=True, photos=_photos(tmp_path))
    paths = client.paths
    paths.events_file.parent.mkdir(parents=True, exist_ok=True)
    # docs: an older failed run, then a good run, then a newer failed run
    _event(paths, 600, "pair:docs", "delete_brake")
    _event(paths, 500, "pair:docs", "sync_started", Level.INFO)
    _event(paths, 490, "pair:docs", "file_failed")  # during the good run: still shown
    _event(paths, 480, "pair:docs", "sync_finished", Level.INFO)
    _event(paths, 100, "pair:docs", "sync_started", Level.INFO)
    _event(paths, 90, "pair:docs", "sync_failed", Level.ERROR)
    # fotos: indexing wait, then a good run settles it
    _event(paths, 400, "photos:fotos", "icloud_indexing")
    _event(paths, 300, "photos:fotos", "sync_started", Level.INFO)
    _event(paths, 200, "photos:fotos", "sync_finished", Level.INFO)
    # jobs that were deleted
    _event(paths, 50, "pair:gone", "delete_brake")
    _event(paths, 40, "photos:old", "sync_failed", Level.ERROR)
    _event(paths, 30, "service", "config_error", Level.ERROR)

    notices = client.call("status.get")["notices"]
    assert [(n["source"], n["code"]) for n in notices] == [
        ("service", "config_error"),
        ("pair:docs", "sync_failed"),
        ("pair:docs", "file_failed"),
    ]


def test_clear_notices_keeps_the_event_log(client: Client) -> None:
    paths = client.paths
    paths.events_file.parent.mkdir(parents=True, exist_ok=True)
    _event(paths, 60, "service", "config_error", Level.ERROR)
    assert len(client.call("status.get")["notices"]) == 1
    with pytest.raises(ApiError):
        client.call("status.clear_notices")  # changes need POST
    client.call("status.clear_notices", post=True)
    assert client.call("status.get")["notices"] == []
    codes = [e["code"] for e in client.call("log.events")["events"]]
    assert codes == ["notices_cleared", "config_error"]
    record_event(Level.ERROR, "service", "config_error", "new", paths=paths)
    assert [n["message"] for n in client.call("status.get")["notices"]] == ["new"]


def test_clear_the_notices_of_one_job(client: Client, tmp_path: Path) -> None:
    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    client.call("photos.create", post=True, photos=_photos(tmp_path))
    paths = client.paths
    _event(paths, 60, "pair:docs", "delete_brake")
    _event(paths, 50, "photos:fotos", "file_failed")
    client.call("status.clear_notices", post=True, source="pair:docs")
    assert [n["source"] for n in client.call("status.get")["notices"]] == ["photos:fotos"]
    with pytest.raises(ApiError):
        client.call("status.clear_notices", post=True, source="../x")


def _run_event(paths: Paths, age: float, code: str, level: Level = Level.INFO,
               **details: Any) -> None:  # fmt: skip
    event = {"time": time.time() - age, "level": level.value, "source": "photos:fotos",
             "code": code, "message": code, "details": details}  # fmt: skip
    with paths.events_file.open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def test_run_history_of_one_job(client: Client) -> None:
    paths = client.paths
    paths.events_file.parent.mkdir(parents=True, exist_ok=True)
    _run_event(paths, 900, "sync_finished", exit_code=0)  # its start is not in the log
    _run_event(paths, 800, "sync_started", dry_run=False)
    _run_event(paths, 790, "icloud_indexing", Level.WARNING)
    _run_event(paths, 780, "sync_postponed", exit_code=4)
    _run_event(paths, 700, "sync_started", dry_run=True)
    _run_event(paths, 690, "file_failed", Level.WARNING)
    deleted = [{"kind": "delete", "path": "2024/05/a.jpg", "to": ""}]
    _run_event(paths, 680, "sync_summary", counts={"download": 3, "failed": 1}, files=deleted,
               files_total=4)  # fmt: skip
    _run_event(paths, 670, "sync_failed", Level.ERROR, exit_code=1, dry_run=True)
    _run_event(paths, 100, "sync_started", dry_run=False)
    record_event(Level.ERROR, "pair:docs", "file_failed", paths=paths)  # another job

    runs = client.call("log.history", source="photos:fotos")["runs"]
    assert [r["result"] for r in runs] == [None, "sync_failed", "sync_postponed",
                                           "sync_finished"]  # fmt: skip
    running, failed, postponed, finished = runs
    assert running["finished_at"] is None and running["events"] == []
    assert failed["dry_run"] and failed["counts"] == {"download": 3, "failed": 1}
    assert (failed["files"], failed["files_total"]) == (deleted, 4)
    assert (finished["files"], finished["files_total"]) == ([], 0)
    assert failed["level"] == "error"
    assert [e["code"] for e in failed["events"]] == ["file_failed"]
    assert [e["code"] for e in postponed["events"]] == ["icloud_indexing"]
    assert finished["started_at"] is None
    assert len(client.call("log.history", source="photos:fotos", limit=1)["runs"]) == 1
    events = client.call("log.events", source="pair:docs")["events"]
    assert [e["source"] for e in events] == ["pair:docs"]


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
    assert photos["pending"]
    assert photos["job"]["result"] == "ok"
    (request,) = take_requests(client.paths)
    assert request.pair == "photos:fotos"
    assert request.dry_run
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


def test_cancel_a_queued_or_running_sync(client: Client, tmp_path: Path) -> None:
    from homeward.runtime import read_events, take_cancels

    client.call("pairs.create", post=True, pair=_pair(tmp_path))
    client.call("pairs.run", post=True, name="docs")
    assert client.call("status.cancel", post=True, name="docs")["cancelled"] == "queued"
    assert pending_requests(client.paths) == []
    assert take_cancels(client.paths) == []  # nothing for the daemon to stop
    assert read_events(client.paths)[0].code == "sync_cancelled"

    with pytest.raises(ApiError) as excinfo:
        client.call("status.cancel", post=True, name="docs")
    assert _error(excinfo) == "not_running"

    client.paths.status_file.write_text('{"running": "photos:me"}')
    assert client.call("status.cancel", post=True, name="photos:me")["cancelled"] == "running"
    assert [c.pair for c in take_cancels(client.paths)] == ["photos:me"]

    with pytest.raises(ApiError) as excinfo:
        client.call("status.cancel", post=True, name="../config")
    assert _error(excinfo) == "invalid_name"
    with pytest.raises(ApiError) as excinfo:
        client.call("status.cancel", name="docs")
    assert _error(excinfo) == "post_required"


# --- folder picker -------------------------------------------------------------------


@pytest.fixture
def volumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from homeward.api.handlers import folders

    root = tmp_path / "root"
    for folder in (
        "volume1/photo/2024",
        "volume1/photo/@eaDir",
        "volume1/photo/#recycle",
        "volume1/photo/.hidden",
        "volume1/homes/alice",
        "volume1/@appstore",
        "volumeUSB1/usbshare",
        "elsewhere/secret",
    ):
        (root / folder).mkdir(parents=True)
    (root / "volume1" / "photo" / "notes.txt").write_text("not a folder")
    (root / "volume1" / "photo" / "outside").symlink_to(root / "elsewhere")
    (root / "volume1" / "photo" / "alias").symlink_to(root / "volume1" / "homes")
    monkeypatch.setattr(folders, "ROOT", root)
    return root


def _names(listing: dict[str, Any]) -> list[str]:
    return [folder["name"] for folder in listing["folders"]]


def test_folders_lists_shares_then_subfolders(client: Client, volumes: Path) -> None:
    shares = client.call("folders.list")
    assert shares["path"] is None
    assert [f["path"] for f in shares["folders"]] == [
        str(volumes / "volume1" / "homes"),
        str(volumes / "volume1" / "photo"),
        str(volumes / "volumeUSB1" / "usbshare"),
    ]
    photo = client.call("folders.list", path=str(volumes / "volume1" / "photo"))
    # files, DSM folders, the recycle bin, dot folders and links off the volumes are hidden
    assert _names(photo) == ["2024", "alias"]
    assert photo["parent"] is None
    assert photo["writable"]
    year = client.call("folders.list", path=str(volumes / "volume1" / "photo" / "2024"))
    assert year["parent"] == str(volumes / "volume1" / "photo")
    assert year["folders"] == []


def test_folders_stay_inside_the_shares(client: Client, volumes: Path) -> None:
    photo = volumes / "volume1" / "photo"
    cases = {
        "invalid_param": ["relative/path", f"{photo}/../homes"],
        "folder_not_found": [
            str(volumes / "volume1"),
            str(volumes / "elsewhere"),
            str(volumes / "volume1" / "@appstore"),
            str(photo / "@eaDir"),
            str(photo / "outside"),
            str(photo / "missing"),
            str(photo / "notes.txt"),
        ],
    }
    for code, paths in cases.items():
        for path in paths:
            with pytest.raises(ApiError) as excinfo:
                client.call("folders.list", path=path)
            assert _error(excinfo) == code, path
    assert _names(client.call("folders.list", path=str(photo / "alias"))) == ["alice"]


def test_folders_only_shows_what_homeward_may_open(
    client: Client, volumes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    homes = volumes / "volume1" / "homes"
    photo = volumes / "volume1" / "photo"
    real_access = os.access

    def access(path: Any, mode: int, **kwargs: Any) -> bool:
        if Path(path) == homes:
            return False
        if Path(path) == photo and mode & os.W_OK:
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", access)
    shares = client.call("folders.list")["folders"]
    assert [(f["name"], f["writable"]) for f in shares] == [("photo", False), ("usbshare", True)]
    with pytest.raises(ApiError) as excinfo:
        client.call("folders.list", path=str(homes))
    assert _error(excinfo) == "folder_denied"
    assert client.call("folders.list", path=str(photo))["writable"] is False
    with pytest.raises(ApiError) as excinfo:
        client.call("folders.create", post=True, path=str(photo), name="new")
    assert _error(excinfo) == "folder_denied"
    assert not (photo / "new").exists()


def test_folders_create(client: Client, volumes: Path) -> None:
    photo = volumes / "volume1" / "photo"
    created = client.call("folders.create", post=True, path=str(photo), name=" Alice ")
    assert created == {"name": "Alice", "path": str(photo / "Alice"), "writable": True}
    assert (photo / "Alice").is_dir()
    with pytest.raises(ApiError) as excinfo:
        client.call("folders.create", post=True, path=str(photo), name="Alice")
    assert _error(excinfo) == "folder_exists"
    for name in ("", "a/b", "..", "@eaDir", ".x", "#recycle", "x" * 256):
        with pytest.raises(ApiError) as excinfo:
            client.call("folders.create", post=True, path=str(photo), name=name)
        assert _error(excinfo) in ("invalid_param", "missing_param"), name
    with pytest.raises(ApiError) as excinfo:
        client.call("folders.create", path=str(photo), name="get")
    assert _error(excinfo) == "post_required"


def test_photos_cleanup_shows_the_count_and_queues_a_request(
    client: Client, tmp_path: Path
) -> None:
    from homeward.core.config import load_config
    from homeward.core.photo_state import PhotoStateDB

    client.call("accounts.add", post=True, name="me", apple_id="a@b.de", password="pw")
    client.call("photos.create", post=True, photos=_photos(tmp_path))
    assert client.call("status.get")["photos"][0]["gone"] == 0  # no run yet

    config = load_config(client.paths.config)
    with PhotoStateDB(config.photo_state_path("me")) as state:
        for key in ("a", "b", "c"):
            state.record("fotos", key, f"{key}.jpg", 1, None)
        state.mark_gone("fotos", "a")
        state.mark_gone("fotos", "b")
        state.record("other", "x", "x.jpg", 1, None)
        state.mark_gone("other", "x")
    photos = client.call("status.get")["photos"][0]
    assert (photos["gone"], photos["files"], photos["bytes"]) == (2, 3, 3)

    client.call("photos.run", post=True, name="fotos", remove_gone="true")
    (request,) = take_requests(client.paths)
    assert request.pair == "photos:fotos"
    assert request.remove_gone
    assert not request.allow_deletes
