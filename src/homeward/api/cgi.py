"""CGI entry point for the DSM UI: ``/webman/3rdparty/homeward/api.cgi``.

Parameters come from the query string and, for POST, from a form-encoded body.
Every request must carry a valid DSM administrator session (see ``auth``).
"""

import json
import logging
import os
import sys
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, BinaryIO, TextIO
from urllib.parse import parse_qsl

from homeward.api.accounts import AccountBackend, default_backend
from homeward.api.app import build_api
from homeward.api.auth import Authenticator, DsmAuthenticator
from homeward.api.errors import ApiError
from homeward.api.server import Request, error_body
from homeward.runtime import Paths

MAX_BODY = 64 * 1024


def handle(
    environ: Mapping[str, str],
    body: BinaryIO,
    authenticator: Authenticator,
    backend: AccountBackend,
    paths: Paths,
) -> tuple[int, dict[str, Any]]:
    try:
        http_method = environ.get("REQUEST_METHOD", "GET").upper()
        params = dict(parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True))
        if http_method == "POST":
            params |= _form(environ, body)
        user = authenticator.authenticate(environ)
    except ApiError as exc:
        return exc.status, error_body(exc)
    api = build_api(paths, backend)
    request = Request(params.pop("method", ""), http_method, params, user)
    return api.handle(request)


def _form(environ: Mapping[str, str], body: BinaryIO) -> dict[str, str]:
    content_type = environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ApiError("unsupported_body", content_type)
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise ApiError("unsupported_body", "bad Content-Length") from None
    if length > MAX_BODY:
        raise ApiError("body_too_large", status=413)
    try:
        text = body.read(length).decode("utf-8") if length > 0 else ""
    except UnicodeDecodeError:
        raise ApiError("unsupported_body", "body is not UTF-8") from None
    return dict(parse_qsl(text, keep_blank_values=True))


def write_response(status: int, body: dict[str, Any], out: TextIO) -> None:
    out.write(f"Status: {status} {HTTPStatus(status).phrase}\r\n")
    out.write("Content-Type: application/json; charset=utf-8\r\n")
    out.write("Cache-Control: no-store\r\n\r\n")
    out.write(json.dumps(body, ensure_ascii=False))
    out.flush()


def main() -> int:
    paths = Paths.from_env()
    try:
        logging.basicConfig(
            filename=paths.log_file if paths.log_file.parent.is_dir() else None,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s api: %(message)s",
        )
    except OSError:  # e.g. not running as the package user: log to the web server
        logging.basicConfig(level=logging.INFO)
    status, body = handle(
        dict(os.environ), sys.stdin.buffer, DsmAuthenticator(), default_backend(paths), paths
    )
    write_response(status, body, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
