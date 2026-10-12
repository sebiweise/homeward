import io
from pathlib import Path

import pytest

from homeward.providers.drive_api import ROOT_ID, ChunkReader
from homeward.providers.fake_drive import FakeDriveApi
from homeward.providers.icloud_drive import FolderCache, ICloudDriveProvider


@pytest.fixture
def api() -> FakeDriveApi:
    return FakeDriveApi()


def _provider(api: FakeDriveApi, root: str = "Docs", **kwargs: object) -> ICloudDriveProvider:
    return ICloudDriveProvider(api, root, **kwargs)  # type: ignore[arg-type]


def test_lists_files_with_extensions_and_nested_folders(api: FakeDriveApi) -> None:
    api.put("Docs/a.txt", "a", mtime=1_700_000_000.0)
    api.put("Docs/sub/deeper/b.tar.gz", "bb")
    api.put("Docs/README", "r")
    api.put("Other/c.txt", "c")
    files = _provider(api).list_files()
    assert set(files) == {"a.txt", "sub/deeper/b.tar.gz", "README"}
    assert files["a.txt"].size == 1
    assert files["a.txt"].mtime == 1_700_000_000.0
    assert files["sub/deeper/b.tar.gz"].sha256 is None


def test_missing_root_lists_nothing_and_is_created_on_write(api: FakeDriveApi) -> None:
    provider = _provider(api, "New/Folder")
    assert provider.list_files() == {}
    meta = provider.write("x/y.txt", io.BytesIO(b"hi"), mtime=1_700_000_000.0)
    assert api.content("New/Folder/x/y.txt") == b"hi"
    assert provider.list_files() == {"x/y.txt": meta}


def test_whole_drive_skips_app_libraries_and_excludes(api: FakeDriveApi) -> None:
    api.add_app_library("Shortcuts")
    api.put("a.txt", "a")
    api.put(".DS_Store", "x")
    api.put("Folder/._a.txt", "x")
    api.put("Folder/~$doc.docx", "x")
    api.put("build/out.o", "x")
    api.put("keep/b.txt", "b")
    files = _provider(api, "", excludes=["build"]).list_files()
    assert set(files) == {"a.txt", "keep/b.txt"}


def test_walk_batches_folders_per_level(api: FakeDriveApi) -> None:
    for i in range(120):
        api.put(f"Docs/f{i}/x.txt", "x")
    _provider(api).list_files()
    # root lookup (2) + Docs children already fetched + 120 folders in batches of 50 (3)
    assert api.calls["folders"] == 2 + 3


def test_read_and_sha256(api: FakeDriveApi) -> None:
    api.put("Docs/a.txt", "hello")
    api.put("Docs/empty.txt", "")
    provider = _provider(api)
    provider.list_files()
    with provider.open_read("a.txt") as fh:
        assert fh.read() == b"hello"
    with provider.open_read("empty.txt") as fh:
        assert fh.read() == b""  # never asks iCloud for an empty file
    assert provider.sha256("a.txt") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    with pytest.raises(FileNotFoundError):
        provider.open_read("missing.txt")


def test_lookup_without_listing_first(api: FakeDriveApi) -> None:
    api.put("Docs/sub/a.txt", "a")
    with _provider(api).open_read("sub/a.txt") as fh:
        assert fh.read() == b"a"


def test_write_never_clobbers(api: FakeDriveApi) -> None:
    api.put("Docs/a.txt", "one")
    provider = _provider(api)
    provider.list_files()
    with pytest.raises(FileExistsError):
        provider.write("a.txt", io.BytesIO(b"two"), mtime=1.0)
    assert api.content("Docs/a.txt") == b"one"


def test_write_returns_meta_that_the_next_listing_confirms(api: FakeDriveApi) -> None:
    provider = _provider(api)
    provider.list_files()
    meta = provider.write("a/b.txt", io.BytesIO(b"data"), mtime=1_700_000_123.0)
    assert meta.size == 4 and meta.mtime == 1_700_000_123.0
    assert _provider(api).list_files()["a/b.txt"] == meta


def test_failed_upload_leaves_no_file(api: FakeDriveApi) -> None:
    api.fail_uploads.add("big.bin")
    provider = _provider(api)
    provider.list_files()
    with pytest.raises(Exception, match="simulated"):
        provider.write("big.bin", io.BytesIO(b"x" * 100), mtime=1.0)
    assert not api.exists("Docs/big.bin")


def test_write_from_unseekable_stream(api: FakeDriveApi) -> None:
    provider = _provider(api)
    stream = ChunkReader(iter([b"ab", b"cd"]))
    provider.write("s.txt", stream, mtime=1.0)  # type: ignore[arg-type]
    assert api.content("Docs/s.txt") == b"abcd"


def test_delete_goes_to_icloud_trash(api: FakeDriveApi) -> None:
    api.put("Docs/a.txt", "a")
    provider = _provider(api)
    provider.list_files()
    provider.delete("a.txt")
    assert not api.exists("Docs/a.txt")
    assert api.trashed == [("a.txt", b"a")]


def test_move_and_rename(api: FakeDriveApi) -> None:
    api.put("Docs/a.txt", "a")
    api.put("Docs/b.txt", "b")
    provider = _provider(api)
    provider.list_files()
    with pytest.raises(FileExistsError):
        provider.move("a.txt", "b.txt")
    meta = provider.move("a.txt", "sub/c.txt")
    assert meta.path == "sub/c.txt"
    assert api.content("Docs/sub/c.txt") == b"a"
    assert set(provider.list_files()) == {"b.txt", "sub/c.txt"}
    provider.move("b.txt", "d.txt")
    assert api.content("Docs/d.txt") == b"b"


def test_folder_named_like_a_file_is_an_error(api: FakeDriveApi) -> None:
    api.put("Docs/a", "file")
    provider = _provider(api)
    provider.list_files()
    with pytest.raises(NotADirectoryError):
        provider.write("a/b.txt", io.BytesIO(b"x"), mtime=1.0)


# Skipping unchanged folders


def _tree(api: FakeDriveApi) -> None:
    api.put("Docs/top.txt", "t")
    api.put("Docs/a/a1.txt", "1")
    api.put("Docs/a/deep/d1.txt", "d")
    api.put("Docs/b/b1.txt", "b")


def test_unchanged_tree_is_served_from_cache(api: FakeDriveApi, tmp_path: Path) -> None:
    _tree(api)
    cache_file = tmp_path / "cache.json"
    first = _provider(api, cache=FolderCache(cache_file), trust_folder_etags=True).list_files()
    api.fetched.clear()
    again = _provider(api, cache=FolderCache(cache_file), trust_folder_etags=True).list_files()
    assert again == first
    assert api.fetched == [ROOT_ID, api.node("Docs").drivewsid]  # only the path to the root


def test_deep_change_refetches_only_the_changed_branch(api: FakeDriveApi, tmp_path: Path) -> None:
    _tree(api)
    cache = FolderCache(tmp_path / "cache.json")
    _provider(api, cache=cache, trust_folder_etags=True).list_files()
    api.put("Docs/a/deep/d2.txt", "new")
    api.fetched.clear()
    files = _provider(api, cache=cache, trust_folder_etags=True).list_files()
    assert "a/deep/d2.txt" in files
    fetched = set(api.fetched)
    assert api.node("Docs/a/deep").drivewsid in fetched
    assert api.node("Docs/b").drivewsid not in fetched


def test_without_propagated_etags_trusting_the_cache_would_miss_changes(tmp_path: Path) -> None:
    """Why ``trust_folder_etags`` is off until the real behaviour is confirmed."""
    api = FakeDriveApi(propagate_etags=False)
    _tree(api)
    cache = FolderCache(tmp_path / "cache.json")
    _provider(api, cache=cache, trust_folder_etags=True).list_files()
    api.put("Docs/a/deep/d2.txt", "new")
    assert "a/deep/d2.txt" not in _provider(api, cache=cache, trust_folder_etags=True).list_files()
    assert "a/deep/d2.txt" in _provider(api, cache=cache).list_files()


def test_broken_cache_file_is_ignored(api: FakeDriveApi, tmp_path: Path) -> None:
    _tree(api)
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("{not json")
    files = _provider(api, cache=FolderCache(cache_file), trust_folder_etags=True).list_files()
    assert len(files) == 4
    assert cache_file.read_text().startswith("{")
