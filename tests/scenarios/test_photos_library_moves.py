"""A photo moved to another library in iCloud (e.g. into the Shared Library) moves on
the NAS too, instead of being downloaded again and counted as deleted."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import Library, PhotoActionKind, PhotoLayout, PhotoOutcome, backup_photos
from homeward.core.planner import DeleteBrake
from homeward.providers.fake_photos import FakePhotoSource

CREATED = datetime(2024, 5, 17, 12, tzinfo=UTC)


class CountingSource(FakePhotoSource):
    def __init__(self) -> None:
        super().__init__()
        self.downloads: list[str] = []

    def open_read(self, item):  # type: ignore[no-untyped-def]
        self.downloads.append(item.key)
        return super().open_read(item)


@pytest.fixture
def source() -> CountingSource:
    return CountingSource()


@pytest.fixture
def pstate() -> PhotoStateDB:
    return PhotoStateDB(":memory:")


def backup(
    source: FakePhotoSource, root: Path, state: PhotoStateDB, **options: object
) -> PhotoOutcome:
    outcome = backup_photos(
        "photos", source, root, state, today=lambda: date(2026, 10, 11),
        brake=DeleteBrake(max_ratio=1.0), selection="all", **options,  # type: ignore[arg-type]
    )  # fmt: skip
    assert outcome.result is not None and outcome.result.ok, outcome.result
    return outcome


def move_to_shared(source: FakePhotoSource, key: str, asset_id: str = "s1") -> None:
    old = source.items[key]
    data = source.data[key]
    source.remove(key)
    source.add(asset_id, old.filename, data, created=CREATED, library=Library.SHARED,
               albums=old.albums)  # fmt: skip


@pytest.mark.parametrize("reliable_changes", [False, True])
def test_a_photo_moved_into_the_shared_library_moves_on_the_nas(
    source: CountingSource, pstate: PhotoStateDB, tmp_path: Path, reliable_changes: bool
) -> None:
    source.changes_reliable = reliable_changes
    photo = source.add("p1", "IMG_1.HEIC", b"one", created=CREATED, albums=("Urlaub",))
    backup(source, tmp_path, pstate, mirror_deletes=True)
    source.downloads.clear()

    move_to_shared(source, photo.key)
    outcome = backup(source, tmp_path, pstate, mirror_deletes=True)

    assert outcome.plan.counts() == {PhotoActionKind.MOVE: 1}
    assert source.downloads == []
    new = tmp_path / "Geteilte Mediathek/2024/05/IMG_1.HEIC"
    assert new.read_bytes() == b"one"
    assert not (tmp_path / "2024/05/IMG_1.HEIC").exists()
    assert os.path.samefile(tmp_path / "Alben/Urlaub/IMG_1.HEIC", new)
    assert set(pstate.records("photos")) == {"shared/s1/original"}
    assert {key for key, _album in pstate.links("photos").values()} == {"shared/s1/original"}
    pstate.clear_full_listing("photos")
    assert backup(source, tmp_path, pstate, mirror_deletes=True).plan.counts() == {}


def test_without_source_folders_the_file_stays_where_it_is(
    source: CountingSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    layout = PhotoLayout(source_folders=False)
    photo = source.add("p1", "IMG_1.HEIC", b"one", created=CREATED)
    backup(source, tmp_path, pstate, layout=layout)

    move_to_shared(source, photo.key)
    outcome = backup(source, tmp_path, pstate, layout=layout)

    assert outcome.plan.counts() == {PhotoActionKind.MOVE: 1}
    assert (tmp_path / "2024/05/IMG_1.HEIC").read_bytes() == b"one"
    assert pstate.records("photos")["shared/s1/original"].path == "2024/05/IMG_1.HEIC"


def test_different_content_is_a_new_photo_not_a_move(
    source: CountingSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    photo = source.add("p1", "IMG_1.HEIC", b"one", created=CREATED)
    backup(source, tmp_path, pstate)
    source.remove(photo.key)
    source.add("s1", "IMG_1.HEIC", b"other", created=CREATED, library=Library.SHARED)

    outcome = backup(source, tmp_path, pstate)

    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.GONE: 1}
    assert (tmp_path / "2024/05/IMG_1.HEIC").read_bytes() == b"one"  # kept


def test_a_copy_in_both_libraries_is_downloaded_twice(
    source: CountingSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("p1", "IMG_1.HEIC", b"one", created=CREATED)
    backup(source, tmp_path, pstate)
    source.add("s1", "IMG_1.HEIC", b"one", created=CREATED, library=Library.SHARED)

    outcome = backup(source, tmp_path, pstate)

    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}
    assert (tmp_path / "2024/05/IMG_1.HEIC").exists()
