import ctypes
import os
import struct
import sys
from pathlib import Path

import pytest

from homeward.core import files
from homeward.core.files import link_noreplace, rename_noreplace

IN_CREATE = 0x100
IN_MOVED_TO = 0x80


def test_rename_noreplace_moves(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a")
    rename_noreplace(tmp_path / "a", tmp_path / "b")
    assert [p.name for p in tmp_path.iterdir()] == ["b"]


def test_rename_noreplace_never_clobbers(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(FileExistsError):
        rename_noreplace(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "a").read_text() == "a"
    assert (tmp_path / "b").read_text() == "b"


def test_fallback_without_renameat2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "_renameat2", None)
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(FileExistsError):
        rename_noreplace(tmp_path / "a", tmp_path / "b")
    rename_noreplace(tmp_path / "a", tmp_path / "c")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b", "c"]


def test_link_noreplace(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a")
    link_noreplace(tmp_path / "a", tmp_path / "b")
    assert os.path.samefile(tmp_path / "a", tmp_path / "b")
    with pytest.raises(FileExistsError):
        link_noreplace(tmp_path / "a", tmp_path / "b")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]  # no temp left


def _events(fd: int) -> dict[str, int]:
    """Read pending inotify events as name -> OR of their masks."""
    seen: dict[str, int] = {}
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        return seen
    offset = 0
    while offset < len(data):
        _wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
        name = data[offset + 16 : offset + 16 + length].rstrip(b"\0").decode()
        seen[name] = seen.get(name, 0) | mask
        offset += 16 + length
    return seen


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux only")
@pytest.mark.skipif(files._renameat2 is None, reason="no renameat2")
def test_final_name_arrives_as_move(tmp_path: Path) -> None:
    """DSM's indexers (Synology Photos) pick up moved-in files, not hard links."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK)
    assert fd >= 0
    try:
        assert libc.inotify_add_watch(fd, os.fsencode(tmp_path), IN_CREATE | IN_MOVED_TO) >= 0
        (tmp_path / ".a.tmp").write_text("a")
        rename_noreplace(tmp_path / ".a.tmp", tmp_path / "a.jpg")
        link_noreplace(tmp_path / "a.jpg", tmp_path / "b.jpg")
        seen = _events(fd)
    finally:
        os.close(fd)
    assert seen["a.jpg"] & IN_MOVED_TO
    assert seen["b.jpg"] & IN_MOVED_TO
