"""Draws the Homeward app icons (a house with an arrow on a blue tile) as PNGs.

Usage: python tools/make_icons.py package/ui/images
Pure standard library, so it runs anywhere without image packages.
"""

import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 72, 256)
BLUE = (0, 134, 229)
WHITE = (255, 255, 255)
SAMPLES = 4  # per axis, for anti-aliasing


def _inside_tile(x: float, y: float) -> bool:
    r = 0.2
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _inside_house(x: float, y: float) -> bool:
    # roof: triangle from (0.18, 0.5) over (0.5, 0.2) to (0.82, 0.5)
    if 0.2 <= y <= 0.5 and abs(x - 0.5) <= (y - 0.2) / 0.3 * 0.32:
        return True
    # walls with a door cut out
    if 0.28 <= x <= 0.72 and 0.48 <= y <= 0.8:
        return not (0.44 <= x <= 0.56 and 0.62 <= y <= 0.8)
    return False


def _pixel(px: int, py: int, size: int) -> tuple[int, int, int, int]:
    tile = house = 0
    for sy in range(SAMPLES):
        for sx in range(SAMPLES):
            x = (px + (sx + 0.5) / SAMPLES) / size
            y = (py + (sy + 0.5) / SAMPLES) / size
            if _inside_tile(x, y):
                tile += 1
                house += _inside_house(x, y)
    total = SAMPLES * SAMPLES
    if not tile:
        return (0, 0, 0, 0)
    mix = house / tile
    rgb = tuple(round(b + (w - b) * mix) for b, w in zip(BLUE, WHITE, strict=True))
    return (*rgb, round(255 * tile / total))  # type: ignore[return-value]


def png(size: int) -> bytes:
    rows = b"".join(
        b"\x00" + b"".join(bytes(_pixel(x, y, size)) for x in range(size)) for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    body = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body


def main(target: str) -> None:
    out = Path(target)
    out.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (out / f"icon_{size}.png").write_bytes(png(size))


if __name__ == "__main__":
    main(sys.argv[1])
