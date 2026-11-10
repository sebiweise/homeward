"""Standalone web server for the Homeward page, for running outside DSM (Docker).

Serves the page (``package/ui``) and the JSON API on one port, the same API the
DSM package runs as ``api.cgi``. Every request needs HTTP Basic auth with the
configured user and password. ``/webman/login.cgi`` hands the signed-in browser a
token that the page sends as ``X-SYNO-TOKEN`` on every API call, the same CSRF
protection as on DSM: another site can make the browser send the credentials, but
it cannot read the token.
"""

import base64
import binascii
import hmac
import io
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homeward.api.accounts import AccountBackend
from homeward.api.auth import Authenticator
from homeward.api.cgi import handle, write_response
from homeward.api.errors import ApiError
from homeward.runtime import Level, Paths, record_event

log = logging.getLogger("homeward.web")

REALM = "Homeward"
TOKEN_PATH = "/webman/login.cgi"
API_PATH = "/api.cgi"
MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 30  # seconds a client may take to send a request
MAX_FAILURES = 10  # wrong sign-ins in a row from one address before it is locked out
LOCK_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Credentials:
    user: str
    password: str

    def check(self, header: str) -> bool:
        """True if ``header`` is a Basic ``Authorization`` header for these credentials."""
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        user, sep, password = decoded.partition(":")
        # Compare both, always, so the timing does not tell which one was wrong.
        user_ok = hmac.compare_digest(user.encode(), self.user.encode())
        password_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return bool(sep) and user_ok and password_ok


class LoginGuard:
    """Locks an address out for ``LOCK_SECONDS`` after ``MAX_FAILURES`` wrong sign-ins
    in a row, so a weak password cannot be guessed quickly. Shared by all handler
    threads."""

    def __init__(self, paths: Paths, clock: Callable[[], float] = time.monotonic) -> None:
        self._paths = paths
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}  # address -> (count, last)

    def locked(self, client: str) -> bool:
        with self._lock:
            count, last = self._failures.get(client, (0, 0.0))
            if count < MAX_FAILURES:
                return False
            if self._clock() - last < LOCK_SECONDS:
                return True
            del self._failures[client]  # the lock ran out: start counting again
            return False

    def failed(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            # Forget addresses that stopped trying, so the table cannot grow for good.
            for address, (_, last) in list(self._failures.items()):
                if now - last >= LOCK_SECONDS:
                    del self._failures[address]
            count = self._failures.get(client, (0, now))[0] + 1
            self._failures[client] = (count, now)
        if count == MAX_FAILURES:
            log.warning(
                "%d wrong sign-ins from %s: locked out for %d minutes",
                count, client, LOCK_SECONDS // 60,
            )  # fmt: skip
            record_event(
                Level.WARNING, "service", "login_locked", until=time.time() + LOCK_SECONDS,
                paths=self._paths, client=client,
            )  # fmt: skip

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


class PasswordAuthenticator(Authenticator):
    """Basic auth plus the page token, in place of the DSM session check."""

    def __init__(self, credentials: Credentials, token: str) -> None:
        self.credentials = credentials
        self.token = token

    def authenticate(self, environ: Mapping[str, str]) -> str:
        if not self.credentials.check(environ.get("HTTP_AUTHORIZATION", "")):
            raise ApiError("not_authenticated", "wrong user or password", status=403)
        token = environ.get("HTTP_X_SYNO_TOKEN", "")
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            raise ApiError("not_authenticated", "missing or wrong page token", status=403)
        return self.credentials.user


def make_server(
    host: str,
    port: int,
    ui_dir: Path,
    paths: Paths,
    credentials: Credentials,
    backend: Callable[[], AccountBackend],
) -> ThreadingHTTPServer:
    """A server for the page and the API; ``backend`` is called once per API request,
    like the CGI, which starts fresh for every request."""
    token = secrets.token_urlsafe(32)
    authenticator = PasswordAuthenticator(credentials, token)
    guard = LoginGuard(paths)

    class Handler(SimpleHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT  # a client that stops sending must not hold a thread

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(ui_dir), **kwargs)  # type: ignore[arg-type]

        def do_GET(self) -> None:
            if not self._signed_in():
                return
            route = self.path.partition("?")[0]
            if route == API_PATH:
                self._api()
            elif route == TOKEN_PATH:
                self._json(200, {"SynoToken": token})
            else:
                super().do_GET()

        def do_HEAD(self) -> None:
            if self._signed_in():
                super().do_HEAD()

        def do_POST(self) -> None:
            if not self._signed_in():
                return
            if self.path.partition("?")[0] == API_PATH:
                self._api()
            else:
                self.send_error(404)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

        def _signed_in(self) -> bool:
            client = self.client_address[0]
            if guard.locked(client):
                self._json(429, {"success": False, "error": {"code": "login_locked"}})
                return False
            header = self.headers.get("Authorization", "")
            if credentials.check(header):
                guard.succeeded(client)
                return True
            if header:  # a browser asks without credentials first: not a failure
                guard.failed(client)
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _api(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._json(413, {"success": False, "error": {"code": "body_too_large"}})
                return
            environ = {
                "REQUEST_METHOD": self.command,
                "QUERY_STRING": self.path.partition("?")[2],
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length),
                "HTTP_AUTHORIZATION": self.headers.get("Authorization", ""),
                "HTTP_X_SYNO_TOKEN": self.headers.get("X-SYNO-TOKEN", ""),
            }
            body = io.BytesIO(self.rfile.read(length))
            status, data = handle(environ, body, authenticator, backend(), paths)
            out = io.StringIO()
            write_response(status, data, out)
            payload = out.getvalue().partition("\r\n\r\n")[2]
            self._send(status, payload.encode())

        def _json(self, status: int, data: object) -> None:
            self._send(status, json.dumps(data).encode())

        def _send(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            log.debug("%s %s", self.address_string(), format % args)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Server errors belong in the container log; the rest stays at debug level.
            if str(code).isdigit() and int(code) >= 500:
                log.warning("%s %s answered %s", self.command, self.path.partition("?")[0], code)
            else:
                super().log_request(code, size)

    return ThreadingHTTPServer((host, port), Handler)
