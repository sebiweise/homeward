"""Request dispatch for the JSON API.

A request names a method (``pairs.list``, ``accounts.login`` ...) and carries
string parameters. Responses follow DSM's web API shape:
``{"success": true, "data": ...}`` or ``{"success": false, "error": {"code": ...}}``.
Methods that change anything only accept POST.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeward.api.accounts import AccountBackend
from homeward.api.errors import ApiError
from homeward.api.store import ConfigStore
from homeward.runtime import Paths

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    http_method: str
    params: Mapping[str, str]
    user: str

    def get(self, name: str, default: str | None = None) -> str:
        value = self.params.get(name, default)
        if value is None:
            raise ApiError("missing_param", name)
        return value

    def flag(self, name: str) -> bool:
        return self.params.get(name, "false").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class Context:
    paths: Paths
    store: ConfigStore
    accounts: AccountBackend


Handler = Callable[[Context, Request], Any]


@dataclass(frozen=True, slots=True)
class Route:
    handler: Handler
    write: bool


class Api:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.routes: dict[str, Route] = {}

    def add(self, name: str, handler: Handler, *, write: bool = False) -> None:
        self.routes[name] = Route(handler, write)

    def handle(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            route = self.routes.get(request.method)
            if route is None:
                raise ApiError("unknown_method", request.method, status=404)
            if route.write and request.http_method != "POST":
                raise ApiError("post_required", request.method, status=405)
            data = route.handler(self.context, request)
        except ApiError as exc:
            return exc.status, error_body(exc)
        except Exception:
            log.exception("API method %s failed", request.method)
            return 500, error_body(ApiError("internal_error", status=500))
        return 200, {"success": True, "data": data}


def error_body(exc: ApiError) -> dict[str, Any]:
    return {"success": False, "error": {"code": exc.code, "message": exc.message}}
