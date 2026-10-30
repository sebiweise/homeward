"""iCloud Photos backups: one ``[[photos]]`` table per Apple ID and NAS folder.

Every option of the backup is a switch in the UI. Removing a backup only removes
the table; the downloaded photos stay where they are.
"""

import json
from dataclasses import fields
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import check_name, check_nas_folder, without_none
from homeward.api.handlers.settings import parse_interval, parse_limits
from homeward.api.server import Context, Request
from homeward.api.store import RawConfig
from homeward.core.config import DEFAULT_TRASH_DAYS, PhotosConfig
from homeward.core.photos import DEFAULT_WORKERS, MAX_WORKERS, Library, PhotoLayout, check_scheme
from homeward.core.planner import DeleteBrake
from homeward.runtime import RunRequest, request_run

LIBRARIES = tuple(lib.value for lib in Library)
_CORE = {f.name: f.default for f in fields(PhotosConfig)}
_LAYOUT = PhotoLayout()
# option -> default; the same defaults as the photos backup in the core
SWITCHES = {
    **{key: _CORE[key] for key in ("albums", "favorites", "videos", "live_videos",
                                   "raw_alternative", "edited", "xmp", "hidden")},
    "mirror_deletes": _CORE["mirror_deletes"],  # deleted in iCloud: deleted on the NAS too
    "source_folders": _LAYOUT.source_folders,  # own folders for shared library and albums
}  # fmt: skip
# Settings of the "Advanced" section, with the core's defaults.
DEFAULT_BRAKE = DeleteBrake()
ADVANCED = {
    "max_delete_ratio": DEFAULT_BRAKE.max_ratio,
    "max_delete_count": DEFAULT_BRAKE.max_count,
    "trash_days": DEFAULT_TRASH_DAYS,
    "workers": DEFAULT_WORKERS,
}
# New backups run this often unless the user picks another interval: a run that finds
# nothing changed in iCloud costs one request.
NEW_JOB_INTERVAL_MINUTES = 30
FOLDERS = {
    "folder_scheme": _LAYOUT.scheme,
    "shared_folder": _LAYOUT.shared_folder,
    "albums_folder": _LAYOUT.albums_folder,
    "favorites_folder": _LAYOUT.favorites_folder,
    "hidden_folder": _LAYOUT.hidden_folder,
    "shared_albums_folder": _LAYOUT.shared_albums_folder,
}


def to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": entry["name"],
        "account": entry.get("account", "default"),
        "target": entry.get("target", ""),
        "libraries": list(entry.get("libraries", LIBRARIES)),
        "interval_minutes": entry.get("interval_minutes"),
        "window": entry.get("window"),
        "max_rate_kb": entry.get("max_rate_kb"),
        "album_names": list(entry.get("album_names", [])),
    }
    result |= {key: entry.get(key, default) for key, default in SWITCHES.items()}
    result |= {key: entry.get(key, default) for key, default in ADVANCED.items()}
    result |= {key: entry.get(key, default) for key, default in FOLDERS.items()}
    return result


def defaults(_ctx: Context, _request: Request) -> dict[str, Any]:
    return {
        "libraries": list(LIBRARIES), "album_names": [],
        "interval_minutes": NEW_JOB_INTERVAL_MINUTES, **SWITCHES, **ADVANCED, **FOLDERS,
    }  # fmt: skip


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
    """Ask the daemon to back up one photos job now (``dry_run`` only lists changes,
    ``remove_gone`` also deletes the photos found deleted in iCloud, to the trash)."""
    name = request.get("name")
    if not any(job["name"] == name for job in _entries(ctx.store.read())):
        raise ApiError("photos_not_found", name, status=404)
    request_run(
        ctx.paths,
        RunRequest(
            pair=f"photos:{name}",
            dry_run=request.flag("dry_run"),
            allow_deletes=request.flag("allow_deletes"),
            remove_gone=request.flag("remove_gone"),
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
    target = check_nas_folder(ctx, text("target"), "target")
    if not target.is_dir():
        raise ApiError("local_missing", str(target))
    entry: dict[str, Any] = {
        "name": name,
        "account": account,
        "apple_id": accounts[account].get("apple_id", ""),
        "target": str(target),
        "libraries": _libraries(data),
        "interval_minutes": parse_interval(data.get("interval_minutes")),  # None: global
        **parse_limits(data),
    }
    for key, switch in SWITCHES.items():
        enabled = data.get(key, switch)
        if not isinstance(enabled, bool):
            raise ApiError("invalid_param", key)
        entry[key] = enabled
    for key, folder in FOLDERS.items():
        entry[key] = _folder(key, text(key, folder))
    entry |= _advanced(data)
    entry["album_names"] = _album_names(data)
    return entry


def _number(data: dict[str, Any], key: str, kind: type, low: float, high: float) -> Any:
    value = data.get(key, ADVANCED[key])
    if not isinstance(value, int | float) or isinstance(value, bool) or not low <= value <= high:
        raise ApiError("invalid_param", key)
    if kind is int and value != int(value):
        raise ApiError("invalid_param", key)
    return kind(value)


def _advanced(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_delete_ratio": _number(data, "max_delete_ratio", float, 0, 1),
        "max_delete_count": _number(data, "max_delete_count", int, 0, 1_000_000),
        "trash_days": _number(data, "trash_days", int, 0, 3650),
        "workers": _number(data, "workers", int, 1, MAX_WORKERS),
    }


def _album_names(data: dict[str, Any]) -> list[str]:
    """Only these albums become folders (and only they are read); empty: all."""
    names = data.get("album_names", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ApiError("invalid_param", "album_names")
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


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
        check_scheme(scheme)
    except ValueError:
        raise ApiError("invalid_scheme", scheme) from None
