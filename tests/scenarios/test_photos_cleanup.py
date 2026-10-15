"""Cleaning up photos deleted in iCloud on request, and the Synology recycle bin."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB, gone_counts
from homeward.core.photos import PhotoActionKind, PhotoOutcome, backup_photos
from homeward.core.planner import DeleteBrake
from homeward.providers.fake_photos import FakePhotoSource
from homeward.providers.nas import find_recycle_bin

TODAY = date(2026, 10, 11)
TRASH = Path(".homeward-papierkorb") / TODAY.isoformat()


@pytest.fixture
def source() -> FakePhotoSource:
    return FakePhotoSource()


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "me.photos.sqlite3"


@pytest.fixture
def pstate(state_path: Path) -> PhotoStateDB:
    return PhotoStateDB(state_path)


def backup(
    source: FakePhotoSource,
    root: Path,
    state: PhotoStateDB,
    *,
    remove_gone: bool = False,
    mirror: bool = False,
    recycle_bin: Path | None = None,
) -> PhotoOutcome:
    return backup_photos(
        "photos", source, root, state, today=lambda: TODAY, remove_gone=remove_gone,
        mirror_deletes=mirror, selection="personal", recycle_bin=recycle_bin,
        brake=DeleteBrake(max_ratio=1.0),
    )  # fmt: skip


def at(month: int) -> datetime:
    return datetime(2024, month, 1, 12, tzinfo=UTC)


def test_cleanup_removes_photos_deleted_in_icloud_and_their_album_links(
    source: FakePhotoSource, pstate: PhotoStateDB, state_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "Photos"
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1), albums=("Urlaub",))
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2), albums=("Urlaub",))
    backup(source, root, pstate)
    source.remove(gone.key)
    assert backup(source, root, pstate).plan.counts() == {PhotoActionKind.GONE: 1}
    assert gone_counts(state_path) == {"photos": 1}

    outcome = backup(source, root, pstate, remove_gone=True)

    assert outcome.result is not None and outcome.result.ok
    assert outcome.plan.counts() == {PhotoActionKind.DELETE: 1}
    assert not (root / "2024/01/IMG_1.HEIC").exists()
    assert not (root / "Alben/Urlaub/IMG_1.HEIC").exists()
    assert (root / TRASH / "2024/01/IMG_1.HEIC").read_bytes() == b"one"  # recoverable
    assert (root / "Alben/Urlaub/IMG_2.HEIC").exists()
    assert gone_counts(state_path) == {}
    assert backup(source, root, pstate).plan.counts() == {}


def test_cleanup_without_changes_in_icloud(
    source: FakePhotoSource, pstate: PhotoStateDB, state_path: Path, tmp_path: Path
) -> None:
    source.changes_reliable = True  # the run would otherwise have nothing to do
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2))
    backup(source, tmp_path, pstate)
    source.remove(gone.key)
    pstate.clear_full_listing("photos")  # the next run reads everything and marks it gone
    assert backup(source, tmp_path, pstate).plan.counts() == {PhotoActionKind.GONE: 1}

    outcome = backup(source, tmp_path, pstate, remove_gone=True)

    assert outcome.plan.counts() == {PhotoActionKind.DELETE: 1}
    assert not (tmp_path / "2024/01/IMG_1.HEIC").exists()
    assert gone_counts(state_path) == {}


def test_a_photo_back_in_icloud_is_not_cleaned_up(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    backup(source, tmp_path, pstate)
    source.remove(gone.key)
    backup(source, tmp_path, pstate)
    source.add("a1", "IMG_1.HEIC", b"one", created=at(1))  # restored in iCloud

    outcome = backup(source, tmp_path, pstate, remove_gone=True)

    assert outcome.plan.counts() == {PhotoActionKind.RETURNED: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").read_bytes() == b"one"


def test_deletes_go_to_the_synology_recycle_bin_of_the_share(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    share = tmp_path / "volume1" / "home"
    root = share / "Photos"
    (share / "#recycle").mkdir(parents=True)
    bin_ = find_recycle_bin(root)
    assert bin_ == share / "#recycle"
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1), albums=("Urlaub",))
    backup(source, root, pstate, mirror=True, recycle_bin=bin_)
    (bin_ / "Photos/2024/01").mkdir(parents=True)
    (bin_ / "Photos/2024/01/IMG_1.HEIC").write_bytes(b"older")  # deleted earlier

    source.remove(gone.key)
    outcome = backup(source, root, pstate, mirror=True, recycle_bin=bin_)

    assert outcome.plan.counts() == {PhotoActionKind.DELETE: 1}
    assert not (root / "2024/01/IMG_1.HEIC").exists()
    assert not (root / "Alben/Urlaub/IMG_1.HEIC").exists()
    # The same path DSM would use, numbered next to the earlier one
    assert (bin_ / "Photos/2024/01/IMG_1.HEIC").read_bytes() == b"older"
    assert (bin_ / "Photos/2024/01/IMG_1 (2).HEIC").read_bytes() == b"one"
    assert not (root / ".homeward-papierkorb").exists()


def test_an_unusable_recycle_bin_falls_back_to_the_backup_trash(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    root = tmp_path / "Photos"
    elsewhere = tmp_path / "other" / "#recycle"  # does not cover the backup folder
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    backup(source, root, pstate, mirror=True, recycle_bin=elsewhere)
    source.remove(gone.key)

    backup(source, root, pstate, mirror=True, recycle_bin=elsewhere)

    assert (root / TRASH / "2024/01/IMG_1.HEIC").read_bytes() == b"one"
    assert not elsewhere.exists()


def test_gone_counts_of_a_missing_database(tmp_path: Path) -> None:
    assert gone_counts(tmp_path / "missing.sqlite3") == {}
