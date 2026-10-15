"""iCloud Photos backups: one ``[[photos]]`` table per Apple ID and NAS folder.

Every option of the backup is a switch in the UI. Removing a backup only removes
the table; the downloaded photos stay where they are.
"""

import json
import string
from pathlib import Path, PurePosixPath
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import check_name, without_none
from homeward.api.handlers.settings import parse_interval
from homeward.api.server import Context, Request
from homeward.api.store import RawConfig
from homeward.runtime import RunRequest, request_run

LIBRARIES = ("personal", "shared", "shared_albums")
SCHEME_FIELDS = ("year", "month", "day")
# option -> default; the same defaults as the photos backup in the core
SWITCHES = {
    "albums": True,
    "favorites": True,
    "videos": True,
    "live_videos": True,
    "raw_alternative": True,
    "edited": True,
    "xmp": True,
    "hidden": False,
    "mirror_deletes": False,  # photos deleted in iCloud are deleted on the NAS too
    "source_folders": True,  # own folders for the shared library and shared albums
}
FOLDERS = {
    "folder_scheme": "{year}/{month}",
    "shared_folder": "Geteilte Mediathek",
    "albums_folder": "Alben",
    "favorites_folder": "Favoriten",
    "hidden_folder": "Ausgeblendet",
    "shared_albums_folder": "Geteilte Alben",
}


def to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": entry["name"],
        "account": entry.get("account", "default"),
        "target": entry.get("target", ""),
        "libraries": list(entry.get("libraries", LIBRARIES)),
        "interval_minutes": entry.get("interval_minutes"),
    }
    result |= {key: entry.get(key, default) for key, default in SWITCHES.items()}
    result |= {key: entry.get(key, default) for key, default in FOLDERS.items()}
    return result


def defaults(_ctx: Context, _request: Request) -> dict[str, Any]:
    return {"libraries": list(LIBRARIES), **SWITCHES, **FOLDERS}


def list_jobs(ctx: Context, _request: Request) -> dict[str, Any]:
    return {"photos": [to_dict(entry) for entry in _entries(ctx.store.read())]}


def create(ctx: Context, request: Request) -> dict[str, Any]:
    entry = _entry(ctx, request)

    def change(raw: RawConfig) -> None:
        jobs = raw.setdefault("photos", [])
        if any(job.get("name") == entry["name"] for job in jobs):
            raise ApiError("photos_exists", entry["name"])
        jobs.append(without_none(entry))

    ctx.store.update(change)
    return to_dict(entry)


def update(ctx: Context, request: Request) -> dict[str, Any]:
    entry = _entry(ctx, request)
    merged: dict[str, Any] = {}

    def change(raw: RawConfig) -> None:
        jobs = raw.get("photos", [])
        for index, job in enumerate(jobs):
            if isinstance(job, dict) and job.get("name") == entry["name"]:
                merged.update(without_none({**job, **entry}))
                jobs[index] = merged
                return
        raise ApiError("photos_not_found", entry["name"], status=404)

    ctx.store.update(change)
    return to_dict(merged)


def delete(ctx: Context, request: Request) -> dict[str, Any]:
    """Remove a backup from the config. Downloaded photos stay untouched."""
    name = request.get("name")

    def change(raw: RawConfig) -> None:
        jobs = _entries(raw)
        if not any(job["name"] == name for job in jobs):
            raise ApiError("photos_not_found", name, status=404)
        raw["photos"] = [job for job in jobs if job["name"] != name]

    ctx.store.update(change)
    return {"name": name}


def run(ctx: Context, request: Request) -> dict[str, Any]:
    """Ask the daemon to back up one photos job now (``dry_run`` only lists changes)."""
    name = request.get("name")
    if not any(job["name"] == name for job in _entries(ctx.store.read())):
        raise ApiError("photos_not_found", name, status=404)
    request_run(
        ctx.paths,
        RunRequest(
            pair=f"photos:{name}",
            dry_run=request.flag("dry_run"),
            allow_deletes=request.flag("allow_deletes"),
        ),
    )
    return {"name": name, "queued": True}


def _entries(raw: RawConfig) -> list[dict[str, Any]]:
    jobs = raw.get("photos", [])
    if not isinstance(jobs, list):
        raise ApiError("config_invalid", "'photos' must be a list of [[photos]] tables")
    return [job for job in jobs if isinstance(job, dict) and isinstance(job.get("name"), str)]


def _entry(ctx: Context, request: Request) -> dict[str, Any]:
    """Validate the ``photos`` parameter (a JSON object) into a config table."""
    try:
        data = json.loads(request.get("photos"))
    except ValueError:
        raise ApiError("invalid_param", "photos must be a JSON object") from None
    if not isinstance(data, dict):
        raise ApiError("invalid_param", "photos must be a JSON object")

    def text(key: str, default: str | None = None) -> str:
        value = data.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ApiError("missing_param", key)
        return value.strip()

    name = check_name(text("name"), "photos")
    account = check_name(text("account"), "account")
    accounts = {a["name"]: a for a in ctx.store.read().get("account", []) if isinstance(a, dict)}
    if account not in accounts:
        raise ApiError("account_not_found", account)
    target = Path(text("target"))
    if not target.is_absolute() or ".." in target.parts:
        raise ApiError("invalid_param", "target")
    if not target.is_dir():
        raise ApiError("local_missing", str(target))
    entry: dict[str, Any] = {
        "name": name,
        "account": account,
        "apple_id": accounts[account].get("apple_id", ""),
        "target": str(target),
        "libraries": _libraries(data),
        "interval_minutes": parse_interval(data.get("interval_minutes")),  # None: global
    }
    for key, switch in SWITCHES.items():
        enabled = data.get(key, switch)
        if not isinstance(enabled, bool):
            raise ApiError("invalid_param", key)
        entry[key] = enabled
    for key, folder in FOLDERS.items():
        entry[key] = _folder(key, text(key, folder))
    return entry


def _libraries(data: dict[str, Any]) -> list[str]:
    libraries = data.get("libraries", list(LIBRARIES))
    if (
        not isinstance(libraries, list)
        or not libraries
        or any(lib not in LIBRARIES for lib in libraries)
    ):
        raise ApiError("invalid_param", "libraries")
    return [lib for lib in LIBRARIES if lib in libraries]


def _folder(key: str, value: str) -> str:
    if key == "folder_scheme":
        _check_scheme(value)
    elif "/" in value or value in (".", ".."):
        raise ApiError("invalid_param", key)
    return value


def _check_scheme(scheme: str) -> None:
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(scheme) if f is not None}
    except ValueError:
        raise ApiError("invalid_scheme", scheme) from None
    path = PurePosixPath(scheme)
    if not fields <= set(SCHEME_FIELDS) or path.is_absolute() or ".." in path.parts:
        raise ApiError("invalid_scheme", scheme)
