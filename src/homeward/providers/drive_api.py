"""Low-level iCloud Drive web API, the seam between the Drive provider and pyicloud.

``DriveApi`` names the handful of calls the provider needs and speaks in the raw item
dicts iCloud returns. ``PyiCloudDriveApi`` implements it on top of a logged-in
``pyicloud`` Drive service: it reuses pyicloud's authenticated session but sends
uploads itself, streaming the file instead of building the request body in memory.
``FakeDriveApi`` in ``fake_drive.py`` implements it for tests.
"""

import io
import mimetypes
from collections.abc import Callable, Iterator, Sequence
from typing import Any, BinaryIO, Protocol

from homeward.providers.multipart import CHUNK, MultipartFile

ZONE = "com.apple.CloudDocs"
ROOT_ID = f"FOLDER::{ZONE}::root"

Item = dict[str, Any]
"""A node as iCloud lists it: ``drivewsid``, ``docwsid``, ``etag``, ``type``, ``name``,
``extension``, ``size``, ``dateModified``, ``zone`` and, for fetched folders, ``items``."""


class DriveApiError(RuntimeError):
    """iCloud answered a Drive request with an error."""


class DriveApi(Protocol):
    def folders(self, drivewsids: Sequence[str]) -> list[Item]:
        """Fetch folders with their direct children (``items``), in the order asked."""
        ...

    def download(self, docwsid: str, zone: str) -> BinaryIO:
        """Open a file's content as a stream. The caller closes it."""
        ...

    def upload(
        self, folder_docwsid: str, zone: str, name: str, data: BinaryIO, size: int, mtime: float
    ) -> None:
        """Store ``size`` bytes from ``data`` as a new file ``name`` in the folder."""
        ...

    def create_folder(self, parent_drivewsid: str, name: str) -> None: ...

    def trash(self, drivewsid: str, etag: str) -> None:
        """Move a node to "Recently Deleted", where iCloud keeps it for 30 days."""
        ...

    def rename(self, drivewsid: str, etag: str, name: str) -> None: ...

    def move(self, drivewsid: str, etag: str, destination_drivewsid: str) -> None: ...


def item_name(item: Item) -> str:
    """The file name as users see it: iCloud stores the extension separately."""
    name = str(item.get("name") or item.get("drivewsid") or "")
    extension = item.get("extension")
    return f"{name}.{extension}" if extension else name


class ChunkReader(io.RawIOBase):
    """A readable binary stream over an iterator of byte chunks."""

    def __init__(self, chunks: Iterator[bytes], on_close: Callable[[], None] = lambda: None):
        self._chunks = chunks
        self._buffer = b""
        self._on_close = on_close

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        while not self._buffer:
            chunk = next(self._chunks, None)
            if chunk is None:
                return 0
            self._buffer = chunk
        size = min(len(buffer), len(self._buffer))
        buffer[:size] = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return size

    def close(self) -> None:
        if not self.closed:
            self._on_close()
        super().close()


class PyiCloudDriveApi(DriveApi):
    """``DriveApi`` on top of ``PyiCloudService(...).drive``.

    It uses pyicloud's session, service URLs and request parameters, which are not
    public API; ``pyproject.toml`` pins pyicloud to a minor version for that reason.
    """

    def __init__(self, drive: Any) -> None:
        self._drive = drive
        self._session = drive.session
        self._service_root: str = drive.service_root
        self._document_root: str = drive._document_root

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {**self._drive.params, **extra}

    def _post(self, url: str, **kwargs: Any) -> Any:
        response = self._session.post(url, params=kwargs.pop("params", self._params()), **kwargs)
        if not response.ok:
            raise DriveApiError(f"{url.rsplit('/', 1)[-1]}: HTTP {response.status_code}")
        return response.json()

    def folders(self, drivewsids: Sequence[str]) -> list[Item]:
        if not drivewsids:
            return []
        result = self._post(
            f"{self._service_root}/retrieveItemDetailsInFolders",
            json=[{"drivewsid": d, "partialData": False} for d in drivewsids],
        )
        by_id = {item.get("drivewsid"): item for item in result}
        missing = [d for d in drivewsids if d not in by_id or "items" not in by_id[d]]
        if missing:
            raise DriveApiError(f"iCloud did not return folders {missing}")
        return [by_id[d] for d in drivewsids]

    def download(self, docwsid: str, zone: str) -> BinaryIO:
        tokens = self._session.get(
            f"{self._document_root}/ws/{zone}/download/by_id",
            params=self._params(document_id=docwsid),
        )
        if not tokens.ok:
            raise DriveApiError(f"download/by_id: HTTP {tokens.status_code}")
        body = tokens.json()
        token = body.get("data_token") or body.get("package_token") or {}
        if not token.get("url"):
            raise DriveApiError("iCloud returned no download URL")
        response = self._session.get(token["url"], params=self._params(), stream=True)
        if not response.ok:
            response.close()
            raise DriveApiError(f"download: HTTP {response.status_code}")
        reader = ChunkReader(response.iter_content(chunk_size=CHUNK), response.close)
        return io.BufferedReader(reader, CHUNK)

    def upload(
        self, folder_docwsid: str, zone: str, name: str, data: BinaryIO, size: int, mtime: float
    ) -> None:
        # Same three steps as pyicloud's DriveService.send_file, with a streamed body.
        content_type = mimetypes.guess_type(name)[0] or ""
        slot = self._post(
            f"{self._document_root}/ws/{zone}/upload/web",
            params=self._params(**self._drive._get_token_from_cookie()),
            headers={"Content-Type": "text/plain"},
            json={"filename": name, "type": "FILE", "content_type": content_type, "size": size},
        )[0]
        body = MultipartFile(name, name, data, size)
        stored = self._post(
            slot["url"],
            params=None,
            data=body,
            headers={"Content-Type": body.content_type, "Content-Length": str(len(body))},
        )["singleFile"]
        document: dict[str, Any] = {
            "signature": stored["fileChecksum"],
            "wrapping_key": stored["wrappingKey"],
            "reference_signature": stored["referenceChecksum"],
            "size": stored["size"],
        }
        if stored.get("receipt"):  # absent for empty files
            document["receipt"] = stored["receipt"]
        millis = int(mtime * 1000)
        self._post(
            f"{self._document_root}/ws/{zone}/update/documents",
            headers={"Content-Type": "text/plain"},
            json={
                "data": document,
                "command": "add_file",
                "create_short_guid": True,
                "document_id": slot["document_id"],
                "path": {"starting_document_id": folder_docwsid, "path": name},
                "allow_conflict": True,
                "file_flags": {"is_writable": True, "is_executable": False, "is_hidden": False},
                "mtime": millis,
                "btime": millis,
            },
        )

    def create_folder(self, parent_drivewsid: str, name: str) -> None:
        self._drive.create_folders(parent_drivewsid, name)

    def trash(self, drivewsid: str, etag: str) -> None:
        self._drive.move_items_to_trash(drivewsid, etag)

    def rename(self, drivewsid: str, etag: str, name: str) -> None:
        self._drive.rename_items(drivewsid, etag, name)

    def move(self, drivewsid: str, etag: str, destination_drivewsid: str) -> None:
        self._post(
            f"{self._service_root}/moveItems",
            json={
                "destinationDrivewsId": destination_drivewsid,
                "items": [{"drivewsid": drivewsid, "etag": etag, "clientId": drivewsid}],
            },
        )
