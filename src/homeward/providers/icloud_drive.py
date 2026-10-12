"""iCloud Drive provider: one folder in iCloud Drive as a tree of files.

There is no change feed for Drive (ADR 0001), so every run walks the folder tree,
level by level, asking iCloud for many folders per request. ``trust_folder_etags``
would reuse cached children of folders whose etag is unchanged, but it must stay off:
on a real account renaming a file changes no folder etag at all (ADR 0002).
"""

import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from homeward.core.models import FileMeta
from homeward.core.rules import is_excluded
from homeward.providers.drive_api import ROOT_ID, ZONE, DriveApi, DriveApiError, Item, item_name

log = logging.getLogger(__name__)

FILE = "FILE"
FOLDER = "FOLDER"
BATCH = 50
_CACHED_KEYS = ("drivewsid", "docwsid", "etag", "type", "name", "extension", "size",
                "dateModified", "zone")  # fmt: skip


class FolderCache:
    """Children of every folder seen in earlier runs, keyed by drivewsid and etag.

    Stored as one JSON file per account; a missing or unreadable file just means
    every folder is fetched again.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._folders: dict[str, dict[str, Any]] = {}
        if path is not None:
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    self._folders = data
            except (OSError, ValueError):
                pass

    def get(self, drivewsid: str, etag: str) -> list[Item] | None:
        entry = self._folders.get(drivewsid)
        if entry is None or entry.get("etag") != etag:
            return None
        items: list[Item] = entry["items"]
        return items

    def put(self, drivewsid: str, etag: str, items: Iterable[Item]) -> None:
        slim = [{k: item[k] for k in _CACHED_KEYS if k in item} for item in items]
        self._folders[drivewsid] = {"etag": etag, "items": slim}

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(self._folders))
        os.replace(tmp, self.path)


def _split(path: str) -> tuple[tuple[str, ...], str]:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        raise ValueError(f"invalid relative path: {path!r}")
    return parts[:-1], parts[-1]


def _mtime(item: Item) -> float:
    value = item.get("dateModified")
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _meta(path: str, item: Item) -> FileMeta:
    # docwsid + etag: replacing a file (new document) or editing it both change it.
    version = f"{item['docwsid']}:{item['etag']}"
    return FileMeta(path, int(item.get("size") or 0), _mtime(item), version)


def _child(folder: Item, name: str) -> Item | None:
    for item in folder["items"]:
        if item_name(item) == name:
            result: Item = item
            return result
    return None


@contextmanager
def _sized(data: BinaryIO) -> Iterator[tuple[BinaryIO, int]]:
    """Yield the stream with the number of bytes left in it, spooling to disk if unknown."""
    try:
        start = data.tell()
        end = data.seek(0, io.SEEK_END)
        data.seek(start)
        yield data, end - start
        return
    except (AttributeError, OSError):
        pass
    with tempfile.TemporaryFile() as spool:
        shutil.copyfileobj(data, spool, 1024 * 1024)
        size = spool.tell()
        spool.seek(0)
        yield spool, size


class ICloudDriveProvider:
    """Files below ``root`` (a folder path in iCloud Drive, "" for the whole Drive).

    Deleted files go to iCloud's "Recently Deleted" (kept 30 days). New files are
    only added to their folder once the upload is complete, so a failed upload never
    leaves a partial file. Only plain files and folders are synced; app libraries
    and other node types are skipped.
    """

    # iCloud Drive treats "A.txt" and "a.txt" as the same name.
    case_insensitive = True

    def __init__(
        self,
        api: DriveApi,
        root: str,
        *,
        name: str = "icloud",
        excludes: Sequence[str] = (),
        cache: FolderCache | None = None,
        trust_folder_etags: bool = False,
    ) -> None:
        self.name = name
        self.api = api
        self._root_parts = tuple(p for p in root.split("/") if p)
        self._excludes = tuple(excludes)
        self._cache = cache or FolderCache()
        self._trust = trust_folder_etags
        self._index: dict[str, Item] = {}  # relative path -> item from the last walk

    # Provider interface

    def list_files(self) -> dict[str, FileMeta]:
        root = self._find_folder(self._root_parts, create=False)
        self._index = {}
        if root is None:
            return {}
        self._index[""] = root
        files: dict[str, FileMeta] = {}
        skipped: dict[str, int] = {}
        level: list[tuple[str, Item]] = [("", root)]
        while level:
            next_level: list[tuple[str, Item]] = []
            for (prefix, _), children in zip(level, self._children(level), strict=True):
                for child in children:
                    rel = f"{prefix}/{item_name(child)}" if prefix else item_name(child)
                    if is_excluded(rel, self._excludes):
                        continue
                    kind = child.get("type")
                    if kind == FILE:
                        self._index[rel] = child
                        files[rel] = _meta(rel, child)
                    elif kind == FOLDER:
                        self._index[rel] = child
                        next_level.append((rel, child))
                    else:
                        skipped[str(kind)] = skipped.get(str(kind), 0) + 1
            level = next_level
        if skipped:
            log.info("skipped iCloud Drive nodes that are not files or folders: %s", skipped)
        self._cache.save()
        return files

    def open_read(self, path: str) -> BinaryIO:
        item = self._file(path)
        if not item.get("size"):
            return io.BytesIO()  # iCloud rejects downloads of empty files
        return self.api.download(item["docwsid"], item.get("zone", ZONE))

    def write(self, path: str, data: BinaryIO, mtime: float) -> FileMeta:
        parent, name = _split(path)
        folder = self._folder_for_write(parent)
        if _child(folder, name) is not None:
            raise FileExistsError(path)
        with _sized(data) as (stream, size):
            self.api.upload(folder["docwsid"], folder.get("zone", ZONE), name, stream, size, mtime)
        return self._refreshed(folder, path, name)

    def delete(self, path: str) -> None:
        item = self._file(path)
        self.api.trash(item["drivewsid"], item["etag"])
        self._index.pop(path, None)

    def move(self, path: str, new_path: str) -> FileMeta:
        old_parent, old_name = _split(path)
        new_parent, new_name = _split(new_path)
        item = self._file(path)
        target = self._folder_for_write(new_parent)
        if _child(target, new_name) is not None:
            raise FileExistsError(new_path)
        if old_parent != new_parent:
            self.api.move(item["drivewsid"], item["etag"], target["drivewsid"])
            target = self.api.folders([target["drivewsid"]])[0]
            item = self._need(_child(target, old_name), path)
        if old_name != new_name:
            self.api.rename(item["drivewsid"], item["etag"], new_name)
        self._index.pop(path, None)
        return self._refreshed(target, new_path, new_name)

    def sha256(self, path: str) -> str:
        digest = hashlib.sha256()
        with self.open_read(path) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    # Tree walking

    def _children(self, level: Sequence[tuple[str, Item]]) -> list[list[Item]]:
        """Children of each folder: from the item itself, the cache, or a batched fetch."""
        result: list[list[Item] | None] = []
        wanted: list[int] = []
        for i, (_, folder) in enumerate(level):
            items: list[Item] | None = folder.get("items")
            if items is None and self._trust:
                items = self._cache.get(folder["drivewsid"], folder["etag"])
            if items is None:
                wanted.append(i)
            elif "items" in folder:
                self._cache.put(folder["drivewsid"], folder["etag"], items)
            result.append(items)
        for start in range(0, len(wanted), BATCH):
            batch = wanted[start : start + BATCH]
            fetched = self.api.folders([level[i][1]["drivewsid"] for i in batch])
            for i, details in zip(batch, fetched, strict=True):
                # The etag the parent listed is the one the cache is checked against.
                self._cache.put(details["drivewsid"], level[i][1]["etag"], details["items"])
                result[i] = details["items"]
        return [items or [] for items in result]

    def _find_folder(self, parts: Sequence[str], *, create: bool) -> Item | None:
        """Fetch the folder at ``parts`` below the Drive root, creating it if asked."""
        folder = self.api.folders([ROOT_ID])[0]
        for part in parts:
            sub = self._subfolder(folder, part, create=create)
            if sub is None:
                return None
            folder = sub
        return folder

    def _subfolder(self, folder: Item, name: str, *, create: bool) -> Item | None:
        child = _child(folder, name)
        if child is None:
            if not create:
                return None
            self.api.create_folder(folder["drivewsid"], name)
            folder = self.api.folders([folder["drivewsid"]])[0]
            child = self._need(_child(folder, name), name)
        if child.get("type") != FOLDER:
            raise NotADirectoryError(name)
        return self.api.folders([child["drivewsid"]])[0]

    def _folder_for_write(self, parent: Sequence[str]) -> Item:
        """Fetch the folder ``parent`` (relative to the root), creating missing folders."""
        depth = len(parent)
        while depth >= 0:
            known = self._index.get("/".join(parent[:depth]))
            if known is not None and known.get("type", FOLDER) == FOLDER:
                folder = self.api.folders([known["drivewsid"]])[0]
                break
            depth -= 1
        else:
            depth = 0
            found = self._find_folder(self._root_parts, create=True)
            folder = self._need(found, "/".join(self._root_parts))
            self._index[""] = folder
        for i in range(depth, len(parent)):
            folder = self._need(self._subfolder(folder, parent[i], create=True), parent[i])
            self._index["/".join(parent[: i + 1])] = folder
        return folder

    def _file(self, path: str) -> Item:
        item = self._index.get(path)
        if item is None:
            parent, name = _split(path)
            folder = self._find_folder((*self._root_parts, *parent), create=False)
            item = _child(folder, name) if folder else None
        if item is None or item.get("type") != FILE:
            raise FileNotFoundError(path)
        return item

    def _refreshed(self, folder: Item, path: str, name: str) -> FileMeta:
        """Re-read ``folder`` and return the new metadata of ``name`` in it."""
        fresh = self.api.folders([folder["drivewsid"]])[0]
        item = self._need(_child(fresh, name), path)
        self._index[path] = item
        return _meta(path, item)

    @staticmethod
    def _need(item: Item | None, what: str) -> Item:
        if item is None:
            raise DriveApiError(f"{what!r} did not show up in iCloud Drive")
        return item
