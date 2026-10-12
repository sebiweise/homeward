"""A multipart/form-data request body that streams one file from disk.

``requests`` builds a ``files=`` body completely in memory, which cost about twice
the file size in RAM on the NAS (ADR 0001). This body reads the file in chunks while
it is sent and knows its length up front, so ``requests`` sends a ``Content-Length``
instead of chunked transfer encoding.
"""

import secrets
from collections.abc import Iterator
from typing import BinaryIO

CHUNK = 1024 * 1024


def _quote(value: str) -> str:
    # Same escaping as urllib3 (WHATWG HTML standard), which ``requests`` uses.
    return value.translate({10: "%0A", 13: "%0D", 34: "%22"})


class MultipartFile:
    """One ``form-data`` part named ``field`` holding ``size`` bytes of ``data``.

    ``data`` is read from its current position; exactly ``size`` bytes must follow.
    """

    def __init__(
        self,
        field: str,
        filename: str,
        data: BinaryIO,
        size: int,
        boundary: str | None = None,
    ) -> None:
        self.boundary = boundary or secrets.token_hex(16)
        self._head = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{_quote(field)}"; '
            f'filename="{_quote(filename)}"\r\n\r\n'
        ).encode()
        self._tail = f"\r\n--{self.boundary}--\r\n".encode()
        self._data = data
        self._size = size
        self._length = len(self._head) + size + len(self._tail)
        self._parts = self._generate()
        self._pending = b""

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.read(CHUNK):
            yield chunk

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes of the body (all remaining bytes for -1)."""
        want = self._length if size is None or size < 0 else size
        out = bytearray(self._pending)
        while len(out) < want:
            part = next(self._parts, None)
            if part is None:
                break
            out += part
        self._pending = bytes(out[want:])
        return bytes(out[:want])

    def _generate(self) -> Iterator[bytes]:
        yield self._head
        remaining = self._size
        while remaining:
            chunk = self._data.read(min(CHUNK, remaining))
            if not chunk:
                raise OSError(f"file ended {remaining} bytes early while uploading")
            remaining -= len(chunk)
            yield chunk
        yield self._tail
