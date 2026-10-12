"""Photos backup scenarios against the fake iCloud Photos source and a real temp folder."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import (
    FAVORITES_ALBUM,
    Library,
    PhotoActionKind,
    PhotoLayout,
    backup_photos,
    check_scheme,
)
from homeward.providers.fake_photos import FakePhotoSource

TODAY = date(2026, 10, 11)


@pytest.fixture
def source() -> FakePhotoSource:
    return FakePhotoSource()


@pytest.fixture
def pstate() -> PhotoStateDB:
    return PhotoStateDB(":memory:")


def run(
    source: FakePhotoSource,
    root: Path,
    state: PhotoStateDB,
    *,
    dry_run: bool = False,
    layout: PhotoLayout | None = None,
    job: str = "photos",
) -> dict[PhotoActionKind, int]:
    outcome = backup_photos(job, source, root, state, layout, dry_run=dry_run, today=lambda: TODAY)
    if outcome.result is not None:
        assert outcome.result.ok, outcome.result.failed
    return outcome.plan.counts()


def at(year: int, month: int, day: int = 1) -> datetime:
    # Noon UTC lands on the same day in every NAS time zone that matters here.
    return datetime(year, month, day, 12, tzinfo=UTC)


def test_year_month_layout_and_mtime(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_0001.HEIC", b"one", created=at(2023, 1, 15))
    source.add("a2", "IMG_0002.MOV", b"two", created=at(2024, 12, 2))

    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 2}

    first = tmp_path / "2023" / "01" / "IMG_0001.HEIC"
    assert first.read_bytes() == b"one"
    assert (tmp_path / "2024" / "12" / "IMG_0002.MOV").read_bytes() == b"two"
    assert first.stat().st_mtime == at(2023, 1, 15).timestamp()
    assert not list(tmp_path.rglob("*.homeward-tmp"))


def test_custom_scheme(source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path) -> None:
    source.add("a1", "x.jpg", created=at(2023, 1, 15))
    run(source, tmp_path, pstate, layout=PhotoLayout(scheme="{year}/{year}-{month}-{day}"))
    assert (tmp_path / "2023" / "2023-01-15" / "x.jpg").exists()


def test_bad_schemes_are_rejected() -> None:
    for scheme in ("{album}", "/{year}", "../{year}", "{0}"):
        with pytest.raises(ValueError):
            check_scheme(scheme)
    check_scheme("{year}/{month}")


def test_incremental_second_run_lists_nothing(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg")
    run(source, tmp_path, pstate)
    assert source.listed == 1

    outcome = backup_photos("photos", source, tmp_path, pstate)
    assert outcome.plan.unchanged and outcome.plan.up_to_date == 1
    assert source.listed == 1  # cursor unchanged: no listing, no downloads
    assert source.downloads == ["personal/a1/original"]

    source.add("a2", "b.jpg")
    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 1}
    assert source.downloads == ["personal/a1/original", "personal/a2/original"]


def test_missing_nas_file_is_restored(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", b"data", created=at(2024, 5))
    run(source, tmp_path, pstate)
    (tmp_path / "2024" / "05" / "a.jpg").unlink()

    assert run(source, tmp_path, pstate) == {PhotoActionKind.RESTORE: 1}
    assert (tmp_path / "2024" / "05" / "a.jpg").read_bytes() == b"data"


def test_same_name_different_photos_get_numbered(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_0001.JPG", b"first", created=at(2024, 5, 1))
    source.add("a2", "IMG_0001.JPG", b"second", created=at(2024, 5, 2))
    run(source, tmp_path, pstate)
    month = tmp_path / "2024" / "05"
    assert (month / "IMG_0001.JPG").read_bytes() == b"first"
    assert (month / "IMG_0001 (2).JPG").read_bytes() == b"second"

    # Paths stay stable on later runs.
    source.add("a3", "other.jpg", created=at(2024, 5, 3))
    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 1}


def test_foreign_file_on_nas_is_never_overwritten(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    month = tmp_path / "2024" / "05"
    month.mkdir(parents=True)
    (month / "a.jpg").write_bytes(b"mine")
    source.add("a1", "a.jpg", b"icloud", created=at(2024, 5))

    run(source, tmp_path, pstate)
    assert (month / "a.jpg").read_bytes() == b"mine"
    assert (month / "a (2).jpg").read_bytes() == b"icloud"


def test_deleted_in_icloud_stays_on_nas(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    item = source.add("a1", "a.jpg", b"keep", created=at(2024, 5))
    run(source, tmp_path, pstate)
    source.remove(item.key)

    assert run(source, tmp_path, pstate) == {PhotoActionKind.GONE: 1}
    assert (tmp_path / "2024" / "05" / "a.jpg").read_bytes() == b"keep"
    assert pstate.records("photos")[item.key].gone_at is not None

    # Restored in iCloud ("Recently Deleted"): tracked again, not downloaded twice.
    source.add("a1", "a.jpg", b"keep", created=at(2024, 5))
    assert run(source, tmp_path, pstate) == {PhotoActionKind.RETURNED: 1}
    assert pstate.records("photos")[item.key].gone_at is None
    assert len(source.downloads) == 1


def test_changed_original_moves_old_version_to_trash(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    item = source.add("a1", "a.jpg", b"v1", created=at(2024, 5), albums=("Urlaub",))
    run(source, tmp_path, pstate)
    source.edit(item.key, b"version 2")

    assert run(source, tmp_path, pstate) == {PhotoActionKind.REPLACE: 1}
    assert (tmp_path / "2024" / "05" / "a.jpg").read_bytes() == b"version 2"
    trash = tmp_path / ".homeward-papierkorb" / TODAY.isoformat()
    assert (trash / "2024" / "05" / "a.jpg").read_bytes() == b"v1"
    album_copy = tmp_path / "Alben" / "Urlaub" / "a.jpg"
    assert album_copy.read_bytes() == b"version 2"
    assert os.path.samefile(album_copy, tmp_path / "2024" / "05" / "a.jpg")


def test_album_hardlinks_share_disk_space(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", b"x" * 100, created=at(2024, 5), albums=("Urlaub", "Best of/2024"))

    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.LINK: 2}
    primary = tmp_path / "2024" / "05" / "a.jpg"
    for link in (
        tmp_path / "Alben" / "Urlaub" / "a.jpg",
        tmp_path / "Alben" / "Best of" / "2024" / "a.jpg",
    ):
        assert os.path.samefile(link, primary)
    assert primary.stat().st_nlink == 3

    outcome = backup_photos("photos", source, tmp_path, pstate)
    assert outcome.plan.unchanged


def test_leaving_an_album_removes_only_the_link(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    item = source.add("a1", "a.jpg", created=at(2024, 5), albums=("Urlaub",))
    run(source, tmp_path, pstate)
    source.set_albums(item.key, ())

    assert run(source, tmp_path, pstate) == {PhotoActionKind.UNLINK: 1}
    assert not (tmp_path / "Alben" / "Urlaub" / "a.jpg").exists()
    assert (tmp_path / "2024" / "05" / "a.jpg").exists()
    assert pstate.links("photos") == {}


def test_edited_album_file_goes_to_trash_not_deleted(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    item = source.add("a1", "a.jpg", b"orig", created=at(2024, 5), albums=("Urlaub",))
    run(source, tmp_path, pstate)
    link = tmp_path / "Alben" / "Urlaub" / "a.jpg"
    link.unlink()
    link.write_bytes(b"edited by user")  # no longer the same file
    source.set_albums(item.key, ())

    run(source, tmp_path, pstate)
    trash = tmp_path / ".homeward-papierkorb" / TODAY.isoformat()
    assert (trash / "Alben" / "Urlaub" / "a.jpg").read_bytes() == b"edited by user"


def test_deleted_album_link_is_recreated(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", created=at(2024, 5), albums=("Urlaub",))
    run(source, tmp_path, pstate)
    (tmp_path / "Alben" / "Urlaub" / "a.jpg").unlink()

    assert run(source, tmp_path, pstate) == {PhotoActionKind.LINK: 1}


def test_albums_can_be_switched_off(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", albums=("Urlaub",))
    outcome = backup_photos("photos", source, tmp_path, pstate, albums=False)
    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}
    assert not (tmp_path / "Alben").exists()


def test_shared_library_gets_its_own_folder(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", b"mine", created=at(2024, 5))
    source.add("a1", "a.jpg", b"family", created=at(2024, 5), library=Library.SHARED)

    run(source, tmp_path, pstate)
    assert (tmp_path / "2024" / "05" / "a.jpg").read_bytes() == b"mine"
    assert (tmp_path / "Geteilte Mediathek" / "2024" / "05" / "a.jpg").read_bytes() == b"family"


def test_live_photo_keeps_both_files(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"still", created=at(2024, 5))
    source.add("a1", "IMG_1.MOV", b"motion", created=at(2024, 5), resource="original_video")

    run(source, tmp_path, pstate)
    assert (tmp_path / "2024" / "05" / "IMG_1.HEIC").read_bytes() == b"still"
    assert (tmp_path / "2024" / "05" / "IMG_1.MOV").read_bytes() == b"motion"


def test_dry_run_changes_nothing(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", albums=("Urlaub",))
    counts = run(source, tmp_path, pstate, dry_run=True)
    assert counts == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.LINK: 1}
    assert list(tmp_path.iterdir()) == []
    assert pstate.records("photos") == {} and pstate.cursor("photos") is None


def test_failed_download_is_retried_and_cursor_kept(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    good = source.add("a1", "a.jpg", created=at(2024, 5))
    bad = source.add("a2", "b.jpg", created=at(2024, 5), albums=("Urlaub",))
    source.fail_keys.add(bad.key)

    outcome = backup_photos("photos", source, tmp_path, pstate)
    assert outcome.result is not None
    assert {f.action.kind for f in outcome.result.failed} == {
        PhotoActionKind.DOWNLOAD,
        PhotoActionKind.LINK,
    }
    assert not (tmp_path / "2024" / "05" / "b.jpg").exists()
    assert pstate.cursor("photos") is None  # not marked as complete
    assert good.key in pstate.records("photos")

    source.fail_keys.clear()
    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.LINK: 1}


def test_truncated_download_leaves_no_file(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    item = source.add("a1", "a.jpg", b"full content", created=at(2024, 5))
    source.data[item.key] = b"full"  # the stream ends early

    outcome = backup_photos("photos", source, tmp_path, pstate)
    assert outcome.result is not None and len(outcome.result.failed) == 1
    assert "incomplete" in outcome.result.failed[0].error
    assert not (tmp_path / "2024" / "05" / "a.jpg").exists()
    assert not list(tmp_path.rglob("*.homeward-tmp"))


def test_two_apple_ids_keep_separate_state(tmp_path: Path) -> None:
    seb, anna = FakePhotoSource("seb"), FakePhotoSource("anna")
    seb.add("x", "seb.jpg", created=at(2024, 5))
    anna.add("x", "anna.jpg", created=at(2024, 5))  # same asset id in another account

    with (
        PhotoStateDB(tmp_path / "seb.photos.sqlite3") as s1,
        PhotoStateDB(tmp_path / "anna.photos.sqlite3") as s2,
    ):
        run(seb, tmp_path / "Seb", s1, job="seb")
        run(anna, tmp_path / "Anna", s2, job="anna")

    assert (tmp_path / "Seb" / "2024" / "05" / "seb.jpg").exists()
    assert (tmp_path / "Anna" / "2024" / "05" / "anna.jpg").exists()
    assert not (tmp_path / "Seb" / "2024" / "05" / "anna.jpg").exists()


def test_unsafe_names_stay_inside_target(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "../../evil.jpg", created=at(2024, 5), albums=("../..",))
    run(source, tmp_path / "t", pstate)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files and all(p.is_relative_to(tmp_path / "t") for p in files)


def test_favorites_folder_holds_hard_links(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", created=at(2024, 5), albums=("Urlaub", FAVORITES_ALBUM))
    assert run(source, tmp_path, pstate) == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.LINK: 2}
    assert os.path.samefile(tmp_path / "Favoriten" / "a.jpg", tmp_path / "2024" / "05" / "a.jpg")


def test_hidden_photos_get_their_own_tree(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "a.jpg", b"secret", created=at(2024, 5), hidden=True)
    run(source, tmp_path, pstate)
    assert (tmp_path / "Ausgeblendet" / "2024" / "05" / "a.jpg").read_bytes() == b"secret"
    assert not (tmp_path / "2024").exists()


def test_shared_albums_are_stored_per_album(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("g:x", "x.jpg", b"w", library=Library.SHARED_ALBUMS, collection="Hochzeit")
    run(source, tmp_path, pstate)
    assert (tmp_path / "Geteilte Alben" / "Hochzeit" / "x.jpg").read_bytes() == b"w"


def test_raw_jpeg_and_edited_versions_sit_side_by_side(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.DNG", b"raw", created=at(2024, 5))
    source.add("a1", "IMG_1.JPG", b"jpeg", created=at(2024, 5), resource="alternative")
    source.add("a1", "IMG_1 (bearbeitet).JPG", b"edit", created=at(2024, 5), resource="edited")
    run(source, tmp_path, pstate)
    month = tmp_path / "2024" / "05"
    assert sorted(p.name for p in month.iterdir()) == [
        "IMG_1 (bearbeitet).JPG", "IMG_1.DNG", "IMG_1.JPG",
    ]  # fmt: skip


def test_xmp_sidecar_follows_a_numbered_original(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a0", "IMG_1.HEIC", b"other", created=at(2024, 5, 1))
    source.add("a1", "IMG_1.HEIC", b"img", created=at(2024, 5, 2))
    sidecar = source.add(
        "a1", "IMG_1.HEIC.xmp", b"<v1/>", created=at(2024, 5, 2), resource="xmp",
        sidecar_for="original",
    )  # fmt: skip
    run(source, tmp_path, pstate)
    month = tmp_path / "2024" / "05"
    assert (month / "IMG_1 (2).HEIC.xmp").read_bytes() == b"<v1/>"
    assert not (month / "IMG_1.HEIC.xmp").exists()

    # Metadata changed in iCloud (e.g. now a favorite): the old sidecar goes to the trash.
    source.edit(sidecar.key, b"<v2/>")
    assert run(source, tmp_path, pstate) == {PhotoActionKind.REPLACE: 1}
    assert (month / "IMG_1 (2).HEIC.xmp").read_bytes() == b"<v2/>"
