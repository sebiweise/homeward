"""The weekly integrity check of a photos backup."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import PhotoActionKind, PhotoOutcome, backup_photos
from homeward.core.photos.integrity import INTEGRITY_DAYS
from homeward.providers.fake_photos import FakePhotoSource

TODAY = date(2026, 10, 11)
WEEK = INTEGRITY_DAYS * 86400


@pytest.fixture
def source() -> FakePhotoSource:
    source = FakePhotoSource()
    created = datetime(2024, 5, 1, 12, tzinfo=UTC)
    source.add("a1", "a.jpg", b"x" * 100, created=created, albums=("Urlaub",))
    source.add("b1", "b.jpg", b"y" * 50, created=created)
    return source


def _run(source: FakePhotoSource, root: Path, state: PhotoStateDB, at: float) -> PhotoOutcome:
    outcome = backup_photos("photos", source, root, state, today=lambda: TODAY, now=lambda: at)
    assert outcome.result is not None and outcome.result.ok
    return outcome


def test_a_damaged_photo_is_moved_aside_and_downloaded_again(
    source: FakePhotoSource, tmp_path: Path
) -> None:
    state = PhotoStateDB(":memory:")
    first = _run(source, tmp_path, state, 1000.0)
    assert first.integrity is None  # nothing to check yet
    photo, link = tmp_path / "2024" / "05" / "a.jpg", tmp_path / "Alben" / "Urlaub" / "a.jpg"
    with photo.open("r+b") as fh:
        fh.truncate(10)  # e.g. cut off by a crash or overwritten by an app

    assert _run(source, tmp_path, state, 1000.0 + WEEK - 1).integrity is None  # not yet a week

    outcome = _run(source, tmp_path, state, 1000.0 + WEEK)
    assert outcome.integrity is not None
    assert (outcome.integrity.checked, outcome.integrity.damaged) == (2, ["2024/05/a.jpg"])
    assert outcome.plan.full  # the whole library is read before anything is replaced
    assert outcome.plan.counts() == {PhotoActionKind.REPLACE: 1}
    assert photo.read_bytes() == b"x" * 100 and os.path.samefile(photo, link)
    (kept,) = (tmp_path / ".homeward-papierkorb").rglob("a.jpg")
    assert kept.stat().st_size == 10  # the damaged copy is kept, not deleted

    assert _run(source, tmp_path, state, 1000.0 + WEEK + 60).integrity is None


def test_a_damaged_photo_icloud_no_longer_has_stays(
    source: FakePhotoSource, tmp_path: Path
) -> None:
    state = PhotoStateDB(":memory:")
    _run(source, tmp_path, state, 1000.0)
    (key,) = (r.key for r in state.records("photos").values() if r.path.endswith("b.jpg"))
    source.remove(key)
    _run(source, tmp_path, state, 2000.0)  # b.jpg is now gone from iCloud
    photo = tmp_path / "2024" / "05" / "b.jpg"
    photo.write_bytes(b"z")

    outcome = _run(source, tmp_path, state, 1000.0 + WEEK)
    assert outcome.integrity is not None and outcome.integrity.damaged == []
    assert photo.read_bytes() == b"z"


def test_a_damaged_photo_deleted_in_icloud_since_the_last_full_listing_stays(
    source: FakePhotoSource, tmp_path: Path
) -> None:
    """The photo is not marked as gone yet (no complete reading since it was deleted
    in iCloud): the check must not move the only copy away."""
    state = PhotoStateDB(":memory:")
    _run(source, tmp_path, state, 1000.0)
    (key,) = (r.key for r in state.records("photos").values() if r.path.endswith("b.jpg"))
    source.remove(key)
    assert all(r.gone_at is None for r in state.records("photos").values())
    photo = tmp_path / "2024" / "05" / "b.jpg"
    photo.write_bytes(b"z")

    outcome = _run(source, tmp_path, state, 1000.0 + WEEK)
    assert outcome.integrity is not None and outcome.integrity.damaged == ["2024/05/b.jpg"]
    assert outcome.plan.full
    assert outcome.plan.counts() == {PhotoActionKind.GONE: 1}
    assert photo.read_bytes() == b"z"  # still the only copy, left in place
    assert not (tmp_path / ".homeward-papierkorb").exists()
    assert state.records("photos")[key].gone_at is not None


def test_a_failed_repair_is_checked_again_by_the_next_run(
    source: FakePhotoSource, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = PhotoStateDB(":memory:")
    _run(source, tmp_path, state, 1000.0)
    photo = tmp_path / "2024" / "05" / "a.jpg"
    with photo.open("r+b") as fh:
        fh.truncate(10)

    def broken(*_args: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("homeward.core.photos.executor.PhotoExecutor._trash", broken)
        outcome = backup_photos(
            "photos", source, tmp_path, state, today=lambda: TODAY, now=lambda: 1000.0 + WEEK
        )
    assert outcome.result is not None and len(outcome.result.failed) == 1
    assert photo.stat().st_size == 10

    outcome = _run(source, tmp_path, state, 1000.0 + WEEK + 60)
    assert outcome.integrity is not None and outcome.integrity.damaged == ["2024/05/a.jpg"]
    assert photo.read_bytes() == b"x" * 100
