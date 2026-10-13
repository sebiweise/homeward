"""Checks that a request comes from a signed-in DSM administrator.

DSM's ``authenticate.cgi`` validates the session cookie together with the
``SynoToken`` the DSM desktop sends as ``X-SYNO-TOKEN`` header. Requiring that
token is the CSRF protection: another site can make the browser send the cookie,
but it cannot read the token.
"""

import grp
import subprocess
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import quote

from homeward.api.errors import ApiError

AUTHENTICATE_CGI = "/usr/syno/synoman/webman/modules/authenticate.cgi"
ADMIN_GROUP = "administrators"
_PASSED_HEADERS = ("HTTP_COOKIE", "HTTP_HOST", "REMOTE_ADDR", "SERVER_NAME", "SERVER_PORT",
                   "HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP")  # fmt: skip


class Authenticator(Protocol):
    def authenticate(self, environ: Mapping[str, str]) -> str:
        """Return the DSM user name or raise :class:`ApiError`."""
        ...


class DsmAuthenticator(Authenticator):
    def __init__(self, cgi: str = AUTHENTICATE_CGI, admin_group: str = ADMIN_GROUP) -> None:
        self.cgi = cgi
        self.admin_group = admin_group

    def authenticate(self, environ: Mapping[str, str]) -> str:
        token = environ.get("HTTP_X_SYNO_TOKEN", "")
        if not token:
            raise ApiError("not_authenticated", "missing SynoToken", status=403)
        env = {key: environ[key] for key in _PASSED_HEADERS if key in environ}
        env |= {
            "REQUEST_METHOD": "GET",
            "QUERY_STRING": f"SynoToken={quote(token)}",
            "HTTP_X_SYNO_TOKEN": token,
        }
        try:
            result = subprocess.run(
                [self.cgi], env=env, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError("not_authenticated", f"DSM authentication failed: {exc}", 403) from exc
        user = result.stdout.strip()
        if result.returncode != 0 or not user or "\n" in user:
            raise ApiError("not_authenticated", "no valid DSM session", status=403)
        if not self.is_admin(user):
            raise ApiError("not_admin", f"{user} is not an administrator", status=403)
        return user

    def is_admin(self, user: str) -> bool:
        try:
            return user in grp.getgrnam(self.admin_group).gr_mem
        except KeyError:
            return False
