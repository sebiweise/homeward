"""Folder picker: browse the NAS's shared folders and create a folder.

The API runs as the package user ``sc-homeward``, so it only lists what that user can
open; whatever is picked here is a folder the daemon can reach too. Shared folders
live at ``/volumeN/<share>`` on DSM. DSM's own folders (``@eaDir``, ``@appstore``
...), the recycle bin, Homeward's trash and dot folders are never shown or browsed.
"""

import os
from pathlib import Path
from typing import Any

from homeward.api.errors import ApiError
from homeward.api.handlers.common import HIDDEN_FOLDERS
from homeward.api.server import Context, Request

# Where the volumes are mounted; tests and the UI dev server point it elsewhere.
ROOT = Path("/")
MAX_ENTRIES = 2000


def list_folders(_ctx: Context, request: Request) -> dict[str, Any]:
    """Without ``path`` the shared folders, else the subfolders of ``path``."""
    path = request.params.get("path", "").strip()
    if not path:
        shares = [_entry(share) for volume in _volumes() for share in _children(volume)]
        return {"path": None, "parent": None, "folders": shares, "truncated": False}
    folder = _checked(path)
    children = _children(folder)
    parent = folder.parent
    return {
        "path": str(folder),
        "parent": str(parent) if parent not in _volumes() else None,
        "writable": _writable(folder),
        "folders": [_entry(child) for child in children[:MAX_ENTRIES]],
        "truncated": len(children) > MAX_ENTRIES,
    }


def create(_ctx: Context, request: Request) -> dict[str, Any]:
    """Create one new folder ``name`` inside ``path``; existing folders stay as they are."""
    parent = _checked(request.get("path"))
    name = request.get("name").strip()
    if not name or "/" in name or "\0" in name or name in (".", "..") or len(name) > 255:
        raise ApiError("invalid_param", "name")
    if _hidden(name):
        raise ApiError("invalid_param", "name")
    if not _writable(parent):
        raise ApiError("folder_denied", str(parent), status=403)
    folder = parent / name
    try:
        folder.mkdir()
    except FileExistsError:
        raise ApiError("folder_exists", str(folder)) from None
    except OSError as exc:
        raise ApiError("folder_denied", f"{folder}: {exc.strerror}", status=403) from None
    return _entry(folder)


def _volumes() -> list[Path]:
    """Mounted volumes (``/volume1``, ``/volumeUSB1`` ...)."""
    try:
        candidates = sorted(ROOT.glob("volume*"))
    except OSError:
        return []
    return [path for path in candidates if path.is_dir() and not path.is_symlink()]


def _hidden(name: str) -> bool:
    return name.startswith(("@", ".")) or name in HIDDEN_FOLDERS


def _readable(path: Path) -> bool:
    return os.access(path, os.R_OK | os.X_OK)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def _children(folder: Path) -> list[Path]:
    """Visible subfolders the package user may open, sorted by name."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    result = []
    for entry in entries:
        if _hidden(entry.name):
            continue
        path = Path(entry.path)
        try:
            if not entry.is_dir() or (entry.is_symlink() and not _on_volume(path.resolve())):
                continue
        except OSError:
            continue
        if _readable(path):
            result.append(path)
    return sorted(result, key=lambda p: (p.name.casefold(), p.name))


def _checked(raw: str) -> Path:
    """A folder inside a shared folder that the package user may open."""
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ApiError("invalid_param", "path")
    volume = next((v for v in _volumes() if path.is_relative_to(v) and path != v), None)
    if volume is None or any(_hidden(part) for part in path.relative_to(volume).parts):
        raise ApiError("folder_not_found", raw, status=404)
    try:
        real = path.resolve(strict=True)
    except OSError:
        raise ApiError("folder_not_found", raw, status=404) from None
    # A symlink may point anywhere; only follow it while it stays on a volume.
    if not real.is_dir() or not _on_volume(real):
        raise ApiError("folder_not_found", raw, status=404)
    if not _readable(path):
        raise ApiError("folder_denied", raw, status=403)
    return path


def _on_volume(real: Path) -> bool:
    return any(real.is_relative_to(v.resolve()) and real != v.resolve() for v in _volumes())


def _entry(path: Path) -> dict[str, Any]:
    return {"name": path.name, "path": str(path), "writable": _writable(path)}
