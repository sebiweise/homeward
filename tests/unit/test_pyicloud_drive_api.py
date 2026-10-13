"""PyiCloudDriveApi against a recorded stand-in for pyicloud's Drive service."""

import io
from typing import Any

import pytest

from homeward.providers.drive_api import DriveApiError, PyiCloudDriveApi
from homeward.providers.multipart import MultipartFile


class _Response:
    def __init__(self, body: Any = None, status: int = 200, chunks: list[bytes] | None = None):
        self._body = body
        self.status_code = status
        self.ok = status < 400
        self._chunks = chunks or []
        self.closed = False

    def json(self) -> Any:
        return self._body

    def iter_content(self, chunk_size: int) -> Any:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        if isinstance(kwargs.get("data"), MultipartFile):
            kwargs["body"] = kwargs["data"].read()  # what requests would send
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


class _Drive:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.params = {"clientId": "c", "dsid": "1"}
        self.service_root = "https://drivews"
        self._document_root = "https://docws"

    def _get_token_from_cookie(self) -> dict[str, str]:
        return {"token": "t"}


def test_upload_streams_the_body_and_commits_the_document() -> None:
    session = _Session(
        [
            _Response([{"document_id": "D1", "url": "https://content/upload"}]),
            _Response(
                {
                    "singleFile": {
                        "fileChecksum": "fc",
                        "wrappingKey": "wk",
                        "referenceChecksum": "rc",
                        "size": 5,
                        "receipt": "r",
                    }
                }
            ),
            _Response({"status": "OK"}),
        ]
    )
    drive = _Drive(session)
    PyiCloudDriveApi(drive).upload(
        "folderdoc", "com.apple.CloudDocs", "a.txt", io.BytesIO(b"hello"), 5, 1_700_000_000.5
    )
    slot, content, commit = session.calls
    assert slot[1] == "https://docws/ws/com.apple.CloudDocs/upload/web"
    assert slot[2]["params"] == {"clientId": "c", "dsid": "1", "token": "t"}
    assert slot[2]["json"]["size"] == 5
    assert slot[2]["json"]["content_type"] == "text/plain"
    assert drive.params == {"clientId": "c", "dsid": "1"}  # not mutated like pyicloud does

    assert content[1] == "https://content/upload"
    assert isinstance(content[2]["data"], MultipartFile)  # streamed, never a bytes body
    assert content[2]["headers"]["Content-Length"] == str(len(content[2]["body"]))
    assert b"hello" in content[2]["body"]

    body = commit[2]["json"]
    assert body["document_id"] == "D1"
    assert body["path"] == {"starting_document_id": "folderdoc", "path": "a.txt"}
    assert body["data"] == {
        "signature": "fc",
        "wrapping_key": "wk",
        "reference_signature": "rc",
        "size": 5,
        "receipt": "r",
    }
    assert body["mtime"] == 1_700_000_000_500


def test_folders_batches_and_checks_the_answer() -> None:
    items = [{"drivewsid": "B", "items": []}, {"drivewsid": "A", "items": []}]
    session = _Session([_Response(items), _Response([{"drivewsid": "A"}])])
    api = PyiCloudDriveApi(_Drive(session))
    assert [f["drivewsid"] for f in api.folders(["A", "B"])] == ["A", "B"]
    assert session.calls[0][2]["json"] == [
        {"drivewsid": "A", "partialData": False},
        {"drivewsid": "B", "partialData": False},
    ]
    with pytest.raises(DriveApiError, match="did not return"):
        api.folders(["A"])


def test_download_streams_and_closes() -> None:
    content = _Response(chunks=[b"he", b"llo"])
    session = _Session([_Response({"data_token": {"url": "https://cdn/f"}}), content])
    stream = PyiCloudDriveApi(_Drive(session)).download("doc", "com.apple.CloudDocs")
    assert session.calls[0][2]["params"]["document_id"] == "doc"
    assert session.calls[1][2]["stream"] is True
    assert stream.read() == b"hello"
    stream.close()
    assert content.closed


def test_errors_raise() -> None:
    session = _Session([_Response(status=503)])
    api = PyiCloudDriveApi(_Drive(session))
    with pytest.raises(DriveApiError, match="HTTP 503"):
        api.folders(["A"])
