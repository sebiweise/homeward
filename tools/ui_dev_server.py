"""Serves the DSM page locally with the real API, a demo sign-in and no DSM session.

Usage: python tools/ui_dev_server.py [port]   then open http://localhost:8765/
Data lives in a temporary folder; the demo sign-in accepts any password and the
two-factor code 123456. Never run this on the NAS.
"""

import io
import sys
import tempfile
from collections.abc import Mapping
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homeward.api.accounts import LoginResult, SessionState
from homeward.api.cgi import handle, write_response
from homeward.api.errors import ApiError
from homeward.runtime import Paths

UI_DIR = Path(__file__).resolve().parent.parent / "package" / "ui"


class DemoAuth:
    def authenticate(self, environ: Mapping[str, str]) -> str:
        return "admin"


class DemoBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionState] = {}

    def login(self, account: str, apple_id: str, password: str) -> LoginResult:
        self.sessions[account] = SessionState.NEEDS_2FA
        return LoginResult(SessionState.NEEDS_2FA)

    def verify_2fa(self, account: str, code: str) -> LoginResult:
        if code != "123456":
            raise ApiError("code_rejected")
        self.sessions[account] = SessionState.SIGNED_IN
        return LoginResult(SessionState.SIGNED_IN)

    def state(self, account: str) -> SessionState:
        return self.sessions.get(account, SessionState.SIGNED_OUT)

    def logout(self, account: str) -> None:
        self.sessions.pop(account, None)


def serve(port: int) -> None:
    var_dir = Path(tempfile.mkdtemp(prefix="homeward-ui-"))
    paths = Paths(var_dir=var_dir, config=var_dir / "config.toml")
    backend = DemoBackend()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(UI_DIR), **kwargs)  # type: ignore[arg-type]

        def do_GET(self) -> None:
            if self.path.startswith("/api.cgi"):
                self._api()
            else:
                super().do_GET()

        def do_POST(self) -> None:
            self._api()

        def _api(self) -> None:
            _, _, query = self.path.partition("?")
            environ = {
                "REQUEST_METHOD": self.command,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            }
            length = int(environ["CONTENT_LENGTH"] or 0)
            body = io.BytesIO(self.rfile.read(length))
            status, data = handle(environ, body, DemoAuth(), backend, paths)
            out = io.StringIO()
            write_response(status, data, out)
            _, _, payload = out.getvalue().partition("\r\n\r\n")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())

    print(f"Homeward UI on http://localhost:{port}/ (data in {var_dir})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
