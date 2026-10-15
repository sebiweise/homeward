"""Photos deleted in iCloud are removed on the NAS too, when the backup mirrors deletes."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import PhotoActionKind, PhotoOutcome, backup_photos
from homeward.core.planner import DeleteBrake
from homeward.providers.fake_photos import FakePhotoSource

TODAY = date(2026, 10, 11)
TRASH = Path(".homeward-papierkorb") / TODAY.isoformat()


@pytest.fixture
def source() -> FakePhotoSource:
    return FakePhotoSource()


@pytest.fixture
def pstate() -> PhotoStateDB:
    return PhotoStateDB(":memory:")


def backup(
    source: FakePhotoSource,
    root: Path,
    state: PhotoStateDB,
    *,
    selection: str = "personal:111111",
    mirror: bool = True,
    allow: bool = False,
    brake: DeleteBrake | None = None,
) -> PhotoOutcome:
    return backup_photos(
        "photos", source, root, state, today=lambda: TODAY, mirror_deletes=mirror,
        selection=selection, brake=brake or DeleteBrake(max_ratio=1.0), allow_deletes=allow,
    )  # fmt: skip


def at(month: int) -> datetime:
    return datetime(2024, month, 1, 12, tzinfo=UTC)


def test_deleted_photo_and_its_album_link_go_to_the_trash(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1), albums=("Urlaub",))
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2), albums=("Urlaub",))
    backup(source, tmp_path, pstate)

    source.remove(gone.key)
    outcome = backup(source, tmp_path, pstate)

    assert outcome.plan.counts() == {PhotoActionKind.DELETE: 1}
    assert not (tmp_path / "2024/01/IMG_1.HEIC").exists()
    assert not (tmp_path / "Alben/Urlaub/IMG_1.HEIC").exists()
    assert (tmp_path / TRASH / "2024/01/IMG_1.HEIC").read_bytes() == b"one"  # recoverable
    assert (tmp_path / "Alben/Urlaub/IMG_2.HEIC").exists()
    assert gone.key not in pstate.records("photos")
    assert all(key != gone.key for key, _ in pstate.links("photos").values())

    source.add("a1", "IMG_1.HEIC", b"one", created=at(1))  # restored in iCloud
    assert backup(source, tmp_path, pstate).plan.counts() == {PhotoActionKind.DOWNLOAD: 1}


def test_without_the_option_nothing_is_deleted(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    backup(source, tmp_path, pstate, mirror=False)
    source.remove(gone.key)
    assert backup(source, tmp_path, pstate, mirror=False).plan.counts() == {PhotoActionKind.GONE: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").exists()


def test_a_changed_selection_does_not_delete(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2))
    backup(source, tmp_path, pstate)

    source.remove(gone.key)  # e.g. videos switched off: they vanish from the listing
    changed = backup(source, tmp_path, pstate, selection="personal:011111")
    assert changed.plan.counts() == {PhotoActionKind.GONE: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").exists()

    source.add("a3", "IMG_3.HEIC", b"three", created=at(3))
    later = backup(source, tmp_path, pstate, selection="personal:011111")
    assert later.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}  # gone ones stay
    assert (tmp_path / "2024/01/IMG_1.HEIC").exists()


def test_first_run_with_a_new_version_does_not_delete(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    backup(source, tmp_path, pstate, selection=None)  # type: ignore[arg-type]
    source.remove(gone.key)
    assert backup(source, tmp_path, pstate).plan.counts() == {PhotoActionKind.GONE: 1}


def test_delete_brake_holds_deletions_until_approved(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    brake = DeleteBrake(max_ratio=1.0, max_count=1)
    items = [source.add(f"a{n}", f"IMG_{n}.HEIC", created=at(n)) for n in range(1, 5)]
    backup(source, tmp_path, pstate, brake=brake)

    source.remove(items[0].key)
    source.remove(items[1].key)
    source.add("a9", "IMG_9.HEIC", created=at(9))
    held = backup(source, tmp_path, pstate, brake=brake)

    assert held.plan.blocked == "2 deletions exceed the limit of 1 files"
    assert held.result is not None and held.result.blocked
    assert (tmp_path / "2024/09/IMG_9.HEIC").exists()  # the rest of the run went on
    assert (tmp_path / "2024/01/IMG_1.HEIC").exists()
    assert pstate.full_listing_at("photos") == 0  # approving reads everything again

    again = backup(source, tmp_path, pstate, brake=brake)
    assert again.plan.blocked  # still held without approval

    approved = backup(source, tmp_path, pstate, brake=brake, allow=True)
    assert approved.plan.counts() == {PhotoActionKind.DELETE: 2}
    assert approved.result is not None and approved.result.ok
    assert not (tmp_path / "2024/01/IMG_1.HEIC").exists()
    assert (tmp_path / TRASH / "2024/02/IMG_2.HEIC").exists()


def test_quick_runs_never_delete(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.changes_reliable = True
    gone = source.add("a1", "IMG_1.HEIC", b"one", created=at(1))
    backup(source, tmp_path, pstate)
    source.remove(gone.key)
    source.add("a2", "IMG_2.HEIC", b"two", created=at(2))
    quick = backup(source, tmp_path, pstate)
    assert not quick.plan.full
    assert quick.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}
    assert (tmp_path / "2024/01/IMG_1.HEIC").exists()
