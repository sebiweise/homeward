"""In-memory iCloud Drive server for tests, behind the ``DriveApi`` interface."""

import io
import itertools
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import BinaryIO

from homeward.providers.drive_api import ROOT_ID, ZONE, DriveApi, DriveApiError, Item

_ids = itertools.count(1)


@dataclass(slots=True)
class _Node:
    drivewsid: str
    docwsid: str
    kind: str  # FILE, FOLDER or APP_LIBRARY
    name: str
    parent: str | None
    etag: int = 1
    data: bytes = b""
    mtime: float = 0.0
    children: list[str] = field(default_factory=list)


class FakeDriveApi(DriveApi):
    """Folders and files keyed by drivewsid, with etags like iCloud's.

    A change to a folder's direct children bumps that folder's etag; with
    ``propagate_etags`` it also bumps every ancestor's. ``calls`` counts API calls
    and ``fetched`` records every folder fetched, so tests can check what a walk cost.
    """

    def __init__(self, *, propagate_etags: bool = True) -> None:
        self.propagate_etags = propagate_etags
        self.nodes: dict[str, _Node] = {ROOT_ID: _Node(ROOT_ID, "root", "FOLDER", "root", None)}
        self.trashed: list[tuple[str, bytes]] = []
        self.calls: Counter[str] = Counter()
        self.fetched: list[str] = []
        self.fail_uploads: set[str] = set()

    # Test helpers: a user changing iCloud Drive from another device

    def put(self, path: str, data: bytes | str, mtime: float = 1_700_000_000.0) -> None:
        raw = data.encode() if isinstance(data, str) else data
        *parents, name = path.split("/")
        folder = self.mkdirs("/".join(parents))
        existing = self._find(folder, name)
        if existing is not None:
            existing.data, existing.mtime = raw, mtime
            existing.etag += 1
            self._touch(folder)
        else:
            self._add(folder, name, "FILE", raw, mtime)

    def mkdirs(self, path: str) -> _Node:
        folder = self.nodes[ROOT_ID]
        for part in [p for p in path.split("/") if p]:
            child = self._find(folder, part)
            folder = child if child is not None else self._add(folder, part, "FOLDER")
        return folder

    def add_app_library(self, name: str) -> None:
        self._add(self.nodes[ROOT_ID], name, "APP_LIBRARY")

    def remove(self, path: str) -> None:
        node = self.node(path)
        parent = self.nodes[node.parent or ROOT_ID]
        parent.children.remove(node.drivewsid)
        del self.nodes[node.drivewsid]
        self._touch(parent)

    def node(self, path: str) -> _Node:
        node = self.nodes[ROOT_ID]
        for part in [p for p in path.split("/") if p]:
            child = self._find(node, part)
            if child is None:
                raise KeyError(path)
            node = child
        return node

    def content(self, path: str) -> bytes:
        return self.node(path).data

    def exists(self, path: str) -> bool:
        try:
            self.node(path)
        except KeyError:
            return False
        return True

    def etag(self, path: str) -> int:
        return self.nodes[ROOT_ID].etag if path == "" else self.node(path).etag

    # DriveApi

    def folders(self, drivewsids: Sequence[str]) -> list[Item]:
        self.calls["folders"] += 1
        self.fetched.extend(drivewsids)
        result = []
        for drivewsid in drivewsids:
            node = self.nodes[drivewsid]
            item = self._item(node)
            item["items"] = [self._item(self.nodes[c]) for c in node.children]
            result.append(item)
        return result

    def download(self, docwsid: str, zone: str) -> BinaryIO:
        self.calls["download"] += 1
        node = next(n for n in self.nodes.values() if n.docwsid == docwsid)
        if not node.data:
            raise DriveApiError("HTTP 400 for an empty file")
        return io.BytesIO(node.data)

    def upload(
        self, folder_docwsid: str, zone: str, name: str, data: BinaryIO, size: int, mtime: float
    ) -> None:
        self.calls["upload"] += 1
        if name in self.fail_uploads:
            data.read(size // 2)
            raise DriveApiError("simulated upload failure")
        raw = data.read(size)
        if len(raw) != size:
            raise DriveApiError("short upload")
        folder = next(n for n in self.nodes.values() if n.docwsid == folder_docwsid)
        self._add(folder, name, "FILE", raw, mtime)

    def create_folder(self, parent_drivewsid: str, name: str) -> None:
        self.calls["create_folder"] += 1
        self._add(self.nodes[parent_drivewsid], name, "FOLDER")

    def trash(self, drivewsid: str, etag: str) -> None:
        self.calls["trash"] += 1
        node = self._checked(drivewsid, etag)
        parent = self.nodes[node.parent or ROOT_ID]
        parent.children.remove(drivewsid)
        del self.nodes[drivewsid]
        self.trashed.append((node.name, node.data))
        self._touch(parent)

    def rename(self, drivewsid: str, etag: str, name: str) -> None:
        self.calls["rename"] += 1
        node = self._checked(drivewsid, etag)
        node.name = name
        node.etag += 1  # like iCloud: no folder etag changes on a rename (ADR 0002)

    def move(self, drivewsid: str, etag: str, destination_drivewsid: str) -> None:
        self.calls["move"] += 1
        node = self._checked(drivewsid, etag)
        old = self.nodes[node.parent or ROOT_ID]
        old.children.remove(drivewsid)
        new = self.nodes[destination_drivewsid]
        new.children.append(drivewsid)
        node.parent = destination_drivewsid
        node.etag += 1
        self._touch(old)
        self._touch(new)

    # Internals

    def _item(self, node: _Node) -> Item:
        name, extension = node.name, None
        if node.kind == "FILE" and "." in node.name.lstrip("."):
            name, extension = node.name.rsplit(".", 1)
        item: Item = {
            "drivewsid": node.drivewsid,
            "docwsid": node.docwsid,
            "etag": f"{node.etag}::x",
            "type": node.kind,
            "name": name,
            "zone": ZONE,
        }
        if extension is not None:
            item["extension"] = extension
        if node.kind == "FILE":
            item["size"] = len(node.data)
            item["dateModified"] = _iso(node.mtime)
        return item

    def _find(self, folder: _Node, name: str) -> _Node | None:
        for child_id in folder.children:
            if self.nodes[child_id].name == name:
                return self.nodes[child_id]
        return None

    def _add(
        self, folder: _Node, name: str, kind: str, data: bytes = b"", mtime: float = 0.0
    ) -> _Node:
        if self._find(folder, name) is not None:
            raise DriveApiError(f"{name} exists")
        number = next(_ids)
        docwsid = f"doc-{number}"
        node = _Node(f"{kind}::{ZONE}::{docwsid}", docwsid, kind, name, folder.drivewsid,
                     data=data, mtime=mtime)  # fmt: skip
        self.nodes[node.drivewsid] = node
        folder.children.append(node.drivewsid)
        self._touch(folder)
        return node

    def _touch(self, folder: _Node) -> None:
        folder.etag += 1
        if self.propagate_etags and folder.parent is not None:
            self._touch(self.nodes[folder.parent])

    def _checked(self, drivewsid: str, etag: str) -> _Node:
        node = self.nodes.get(drivewsid)
        if node is None:
            raise DriveApiError(f"{drivewsid} does not exist")
        if f"{node.etag}::x" != etag:
            raise DriveApiError(f"etag conflict for {node.name}")
        return node


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
