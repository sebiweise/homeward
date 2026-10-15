"""Runs that read only what changed in iCloud, and the full reading that backs them up."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import (
    FULL_LISTING_HOURS,
    PhotoActionKind,
    PhotoOutcome,
    backup_photos,
)
from homeward.providers.fake_photos import FakePhotoSource

TODAY = date(2026, 10, 11)
HOUR = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def source() -> FakePhotoSource:
    fake = FakePhotoSource()
    fake.changes_reliable = True
    return fake


@pytest.fixture
def pstate() -> PhotoStateDB:
    return PhotoStateDB(":memory:")


@pytest.fixture
def clock() -> Clock:
    return Clock()


def backup(
    source: FakePhotoSource, root: Path, state: PhotoStateDB, clock: Clock, *, ok: bool = True
) -> PhotoOutcome:
    outcome = backup_photos(
        "photos", source, root, state, today=lambda: TODAY, now=clock, workers=2
    )
    assert outcome.result is not None
    assert outcome.result.ok is ok, outcome.result.failed
    clock.now += HOUR
    return outcome


def at(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, 12, tzinfo=UTC)


def test_new_photo_is_fetched_without_reading_the_library(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5), albums=("Urlaub",))
    first = backup(source, tmp_path, pstate, clock)
    assert first.plan.full
    assert source.listed == 1

    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 2, 7))
    second = backup(source, tmp_path, pstate, clock)

    assert not second.plan.full
    assert source.listed == 1  # the library was not read again
    assert second.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}
    assert second.plan.up_to_date == 1
    assert (tmp_path / "2024/02/IMG_2.HEIC").read_bytes() == b"two"
    assert (tmp_path / "Alben/Urlaub/IMG_1.HEIC").exists()  # untouched


def test_edited_photo_is_replaced_and_its_album_link_follows(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    item = source.add("a1", "IMG_1.HEIC", b"v1", created=at(2024, 1, 5), albums=("Urlaub",))
    backup(source, tmp_path, pstate, clock)

    source.edit(item.key, b"v2")
    outcome = backup(source, tmp_path, pstate, clock)

    assert not outcome.plan.full
    assert outcome.plan.counts() == {PhotoActionKind.REPLACE: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").read_bytes() == b"v2"
    assert (tmp_path / "Alben/Urlaub/IMG_1.HEIC").read_bytes() == b"v2"
    trash = tmp_path / ".homeward-papierkorb" / TODAY.isoformat() / "2024/01/IMG_1.HEIC"
    assert trash.read_bytes() == b"v1"  # the old version stays recoverable


def test_removed_photo_is_marked_by_the_next_full_reading(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    item = source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 1, 6))
    backup(source, tmp_path, pstate, clock)

    source.remove(item.key)
    quick = backup(source, tmp_path, pstate, clock)
    assert not quick.plan.full
    assert quick.plan.counts() == {}

    clock.now += FULL_LISTING_HOURS * HOUR
    source.add("a3", "IMG_3.HEIC", b"three", created=at(2024, 1, 7))
    full = backup(source, tmp_path, pstate, clock)
    assert full.plan.full
    assert full.plan.counts() == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.GONE: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").read_bytes() == b"one"  # a backup never deletes


def test_album_changes_read_the_whole_library(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    item = source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5), albums=("Alt",))
    backup(source, tmp_path, pstate, clock)

    source.set_albums(item.key, ("Neu",))
    outcome = backup(source, tmp_path, pstate, clock)

    assert outcome.plan.full
    assert source.listed == 2
    assert (tmp_path / "Alben/Neu/IMG_1.HEIC").exists()
    assert not (tmp_path / "Alben/Alt/IMG_1.HEIC").exists()


def test_a_file_missing_on_the_nas_reads_the_whole_library(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    backup(source, tmp_path, pstate, clock)
    (tmp_path / "2024/01/IMG_1.HEIC").unlink()

    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 1, 6))
    outcome = backup(source, tmp_path, pstate, clock)

    assert outcome.plan.full
    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1, PhotoActionKind.RESTORE: 1}


def test_changes_are_only_trusted_after_a_complete_run_without_errors(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    source.fail_keys.add("personal/a1/original")
    assert backup(source, tmp_path, pstate, clock, ok=False).plan.full

    source.fail_keys.clear()
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 1, 6))
    retry = backup(source, tmp_path, pstate, clock)
    assert retry.plan.full  # the failed run left no complete reading behind
    assert retry.plan.counts() == {PhotoActionKind.DOWNLOAD: 2}
    assert source.changes_listed == 0


def test_a_failed_quick_run_is_repeated_from_the_same_point(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    backup(source, tmp_path, pstate, clock)

    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 1, 6))
    source.fail_keys.add("personal/a2/original")
    assert not backup(source, tmp_path, pstate, clock, ok=False).plan.full

    source.fail_keys.clear()
    source.add("a3", "IMG_3.HEIC", b"three", created=at(2024, 1, 7))
    outcome = backup(source, tmp_path, pstate, clock)
    assert not outcome.plan.full
    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 2}  # a2 again, and a3
    assert (tmp_path / "2024/01/IMG_2.HEIC").read_bytes() == b"two"


def test_unreliable_change_feed_reads_the_whole_library(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    backup(source, tmp_path, pstate, clock)

    source.changes_reliable = False
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2024, 1, 6))
    outcome = backup(source, tmp_path, pstate, clock)
    assert outcome.plan.full
    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}


def test_new_sidecar_of_an_earlier_photo_lands_next_to_it(
    source: FakePhotoSource, pstate: PhotoStateDB, clock: Clock, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"old", created=at(2024, 1, 5))  # takes the plain name
    photo = source.add("a2", "IMG_1.HEIC", b"one", created=at(2024, 1, 5))
    backup(source, tmp_path, pstate, clock)

    source.add(
        "a2", "IMG_1.xmp", b"<x/>", created=at(2024, 1, 5), resource="xmp",
        sidecar_for="original",
    )  # fmt: skip
    outcome = backup(source, tmp_path, pstate, clock)

    assert not outcome.plan.full
    assert pstate.records("photos")[photo.key].path == "2024/01/IMG_1 (2).HEIC"
    assert (tmp_path / "2024/01/IMG_1 (2).HEIC.xmp").read_bytes() == b"<x/>"
