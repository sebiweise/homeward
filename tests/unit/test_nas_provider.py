import io
from datetime import date
from pathlib import Path

import pytest

from homeward.providers.nas import NasProvider


@pytest.fixture
def nas(tmp_path: Path) -> NasProvider:
    return NasProvider(tmp_path, today=lambda: date(2026, 10, 11))


def test_write_list_read(nas: NasProvider, tmp_path: Path) -> None:
    meta = nas.write("a/b.txt", io.BytesIO(b"hello"), mtime=1_700_000_000.0)
    assert meta.size == 5 and meta.mtime == 1_700_000_000.0
    assert nas.list_files() == {"a/b.txt": meta}
    with nas.open_read("a/b.txt") as fh:
        assert fh.read() == b"hello"
    assert nas.sha256("a/b.txt") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert [p.name for p in (tmp_path / "a").iterdir()] == ["b.txt"]  # no temp files left


def test_write_never_clobbers(nas: NasProvider) -> None:
    nas.write("a.txt", io.BytesIO(b"one"), mtime=1.0)
    with pytest.raises(FileExistsError):
        nas.write("a.txt", io.BytesIO(b"two"), mtime=1.0)
    with nas.open_read("a.txt") as fh:
        assert fh.read() == b"one"


def test_delete_moves_to_trash(nas: NasProvider, tmp_path: Path) -> None:
    nas.write("d/a.txt", io.BytesIO(b"1"), mtime=1.0)
    nas.delete("d/a.txt")
    nas.write("d/a.txt", io.BytesIO(b"2"), mtime=1.0)
    nas.delete("d/a.txt")
    day = tmp_path / ".homeward-papierkorb" / "2026-10-11" / "d"
    assert (day / "a.txt").read_bytes() == b"1"
    assert (day / "a.txt (2)").read_bytes() == b"2"
    assert nas.list_files() == {}  # the trash and Synology folders are not listed


def test_move(nas: NasProvider) -> None:
    nas.write("a.txt", io.BytesIO(b"1"), mtime=1.0)
    nas.write("b.txt", io.BytesIO(b"2"), mtime=1.0)
    with pytest.raises(FileExistsError):
        nas.move("a.txt", "b.txt")
    meta = nas.move("a.txt", "sub/c.txt")
    assert set(nas.list_files()) == {"b.txt", "sub/c.txt"}
    assert meta.path == "sub/c.txt"


def test_lists_skip_excluded_and_symlinks(nas: NasProvider, tmp_path: Path) -> None:
    (tmp_path / "@eaDir").mkdir()
    (tmp_path / "@eaDir" / "x").write_text("x")
    (tmp_path / ".DS_Store").write_text("x")
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    assert set(nas.list_files()) == {"real.txt"}


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", ""])
def test_rejects_paths_outside_root(nas: NasProvider, path: str) -> None:
    with pytest.raises(ValueError):
        nas.open_read(path)
