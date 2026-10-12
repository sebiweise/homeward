import io
from email.parser import BytesParser
from email.policy import HTTP

import pytest

from homeward.providers.multipart import CHUNK, MultipartFile


class _Counting(io.BytesIO):
    """Records the largest single read, to prove the body never slurps the file."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.largest_read = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        self.largest_read = max(self.largest_read, len(chunk))
        return chunk


def test_body_matches_requests_layout() -> None:
    body = MultipartFile("a.txt", "a.txt", io.BytesIO(b"hello"), 5, boundary="XyZ")
    expected = (
        b"--XyZ\r\n"
        b'Content-Disposition: form-data; name="a.txt"; filename="a.txt"\r\n\r\n'
        b"hello\r\n--XyZ--\r\n"
    )
    assert body.content_type == "multipart/form-data; boundary=XyZ"
    assert len(body) == len(expected)
    assert body.read() == expected
    assert body.read(10) == b""


def test_body_parses_as_multipart() -> None:
    data = bytes(range(256)) * 10
    body = MultipartFile('we "ird"\n.txt', "Grüße.txt", io.BytesIO(data), len(data))
    raw = f"Content-Type: {body.content_type}\r\n\r\n".encode() + body.read()
    message = BytesParser(policy=HTTP).parsebytes(raw)
    (part,) = message.iter_parts()  # type: ignore[attr-defined]
    assert part.get_payload(decode=True) == data
    assert 'name="we %22ird%22%0A.txt"' in part["Content-Disposition"]


def test_large_file_is_read_in_chunks() -> None:
    size = 5 * CHUNK + 123
    source = _Counting(b"x" * size)
    body = MultipartFile("big.bin", "big.bin", source, size)
    total = 0
    while chunk := body.read(64 * 1024):
        assert len(chunk) <= 64 * 1024
        total += len(chunk)
    assert total == len(body)
    assert source.largest_read <= CHUNK


def test_iterating_yields_whole_body() -> None:
    body = MultipartFile("f", "f", io.BytesIO(b"abc"), 3, boundary="b")
    same = MultipartFile("f", "f", io.BytesIO(b"abc"), 3, boundary="b")
    assert b"".join(body) == same.read()


def test_short_file_fails_instead_of_sending_a_wrong_length() -> None:
    body = MultipartFile("f", "f", io.BytesIO(b"abc"), 10)
    with pytest.raises(OSError, match="ended 7 bytes early"):
        body.read()
