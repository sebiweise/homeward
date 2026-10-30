import base64
import json
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from homeward import container
from homeward.api.accounts import UnavailableBackend
from homeward.api.errors import ApiError
from homeward.runtime import Paths
from homeward.web import Credentials, PasswordAuthenticator, make_server

CREDENTIALS = Credentials("admin", "s3cret:with colon")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


GOOD = _basic("admin", "s3cret:with colon")


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        (GOOD, True),
        (GOOD.replace("Basic", "basic"), True),
        (_basic("admin", "wrong"), False),
        (_basic("root", "s3cret:with colon"), False),
        ("Basic " + base64.b64encode(b"admin").decode(), False),
        ("Basic not-base64!", False),
        ("Bearer " + GOOD.split()[1], False),
        ("", False),
    ],
)
def test_credentials_check_basic_auth(header: str, ok: bool) -> None:
    assert CREDENTIALS.check(header) is ok


def test_authenticator_needs_password_and_page_token() -> None:
    auth = PasswordAuthenticator(CREDENTIALS, "token")
    assert auth.authenticate({"HTTP_AUTHORIZATION": GOOD, "HTTP_X_SYNO_TOKEN": "token"}) == "admin"
    for environ in (
        {"HTTP_AUTHORIZATION": GOOD},
        {"HTTP_AUTHORIZATION": GOOD, "HTTP_X_SYNO_TOKEN": "other"},
        {"HTTP_X_SYNO_TOKEN": "token"},
    ):
        with pytest.raises(ApiError) as excinfo:
            auth.authenticate(environ)
        assert excinfo.value.code == "not_authenticated"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<p>Homeward</p>")
    paths = Paths(var_dir=tmp_path / "var", config=tmp_path / "var" / "config.toml")
    httpd = make_server("127.0.0.1", 0, ui, paths, CREDENTIALS, UnavailableBackend)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def _status(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> int:
    try:
        return int(_get(url, headers, data)[0])
    except urllib.error.HTTPError as exc:
        return exc.code


def test_server_asks_for_the_password_first(server: str) -> None:
    for path in ("/", "/index.html", "/api.cgi?method=pairs.list", "/webman/login.cgi"):
        assert _status(server + path) == 401
        assert _status(server + path, {"Authorization": _basic("admin", "x")}) == 401
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server + "/")
    assert excinfo.value.headers["WWW-Authenticate"].startswith('Basic realm="Homeward"')


def test_server_serves_page_and_api_with_the_page_token(server: str) -> None:
    auth = {"Authorization": GOOD}
    status, page = _get(server + "/", auth)
    assert (status, page) == (200, b"<p>Homeward</p>")
    token = json.loads(_get(server + "/webman/login.cgi", auth)[1])["SynoToken"]
    assert token

    # signed in, but without the token: refused like a DSM call without SynoToken
    assert _status(server + "/api.cgi?method=pairs.list", auth) == 403

    headers = auth | {"X-SYNO-TOKEN": token}
    status, body = _get(server + "/api.cgi?method=pairs.list", headers)
    assert status == 200
    assert json.loads(body) == {"success": True, "data": {"pairs": []}}

    form = urlencode({"method": "settings.update", "interval_minutes": "30"}).encode()
    post = headers | {"Content-Type": "application/x-www-form-urlencoded"}
    status, body = _get(server + "/api.cgi", post, form)
    assert json.loads(body)["success"], body
    assert _status(server + "/other", post, form) == 404


def test_server_refuses_large_bodies(server: str) -> None:
    headers = {"Authorization": GOOD, "Content-Type": "application/x-www-form-urlencoded"}
    assert _status(server + "/api.cgi", headers, b"x" * (64 * 1024 + 1)) == 413


# --- container entry point -----------------------------------------------------------


def test_container_writes_the_default_config_once(tmp_path: Path) -> None:
    paths = Paths(var_dir=tmp_path / "config", config=tmp_path / "config" / "config.toml")
    assert container.ensure_config(paths)
    config = tomllib.loads(paths.config.read_text())
    assert config["daemon"]["interval_minutes"] == 60
    paths.config.write_text('state_dir = "state"\n')
    assert not container.ensure_config(paths)
    assert paths.config.read_text() == 'state_dir = "state"\n'


def test_container_web_page_needs_a_password(tmp_path: Path) -> None:
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    assert container.start_web(paths, {}) is None
    thread = container.start_web(
        paths,
        {"HOMEWARD_WEB_PASSWORD": "pw", "HOMEWARD_WEB_HOST": "127.0.0.1", "HOMEWARD_WEB_PORT": "0"},
    )
    assert thread is not None
    assert thread.is_alive()


def test_container_runs_the_daemon_on_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("HOMEWARD_VAR", str(tmp_path))
    monkeypatch.delenv("HOMEWARD_CONFIG", raising=False)
    monkeypatch.delenv("HOMEWARD_WEB_PASSWORD", raising=False)
    monkeypatch.setattr(container.scheduler, "main", lambda argv: calls.append(argv) or 0)
    assert container.main(["--once"]) == 0
    assert calls == [["--config", str(tmp_path / "config.toml"), "--once"]]
    assert (tmp_path / "config.toml").exists()
