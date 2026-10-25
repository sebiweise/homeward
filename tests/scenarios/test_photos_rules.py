"""File rules on photos jobs: ignore files, archive old photos (delete them in iCloud)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import (
    FULL_LISTING_HOURS,
    Library,
    PhotoActionKind,
    PhotoOutcome,
    PhotoRules,
    backup_photos,
)
from homeward.core.workflow import FileRule, RuleAction
from homeward.providers.fake_photos import FakePhotoSource

TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC).timestamp()
DAY = 24 * 3600
OLD = datetime(2019, 5, 1, 12, tzinfo=UTC)
NEW = datetime(2026, 9, 1, 12, tzinfo=UTC)

ARCHIVE_OLD = FileRule("alt", RuleAction.ARCHIVE, older_than_days=3 * 365, preview=False)
IGNORE_MOV = FileRule("videos", RuleAction.IGNORE, extensions=("mov",))


@pytest.fixture
def source() -> FakePhotoSource:
    return FakePhotoSource()


@pytest.fixture
def pstate() -> PhotoStateDB:
    return PhotoStateDB(":memory:")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now

    def next_day(self) -> None:
        self.now += FULL_LISTING_HOURS * 3600 + 1


def backup(
    source: FakePhotoSource,
    root: Path,
    state: PhotoStateDB,
    *rules: FileRule,
    clock: Clock | None = None,
    limit: int = 200,
    allow: bool = False,
    mirror: bool = False,
    dry_run: bool = False,
    can_delete: bool = True,
) -> PhotoOutcome:
    return backup_photos(
        "photos", source, root, state, today=lambda: TODAY, now=clock or (lambda: NOW),
        mirror_deletes=mirror, selection="s", allow_deletes=allow, dry_run=dry_run,
        rules=PhotoRules(rules, limit, can_delete),
    )  # fmt: skip


def test_ignored_files_are_not_downloaded(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", created=NEW)
    source.add("a1", "IMG_1.MOV", created=NEW, resource="original_video")
    source.add("a2", "CLIP.mov", created=NEW)

    outcome = backup(source, tmp_path, pstate, IGNORE_MOV)

    assert outcome.plan.counts() == {PhotoActionKind.DOWNLOAD: 1}
    assert sorted(h.path for h in outcome.plan.rule_hits) == ["CLIP.mov", "IMG_1.MOV"]
    assert (tmp_path / "2026/09/IMG_1.HEIC").exists()
    assert not (tmp_path / "2026/09/CLIP.mov").exists()


def test_ignoring_backed_up_photos_never_deletes_them(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a2", "CLIP.mov", created=NEW, albums=("Urlaub",))
    source.add("a3", "IMG_3.HEIC", created=NEW)
    backup(source, tmp_path, pstate, mirror=True)
    source.add("a3", "IMG_3.HEIC", b"new photo moves the cursor", created=NEW)

    outcome = backup(source, tmp_path, pstate, IGNORE_MOV, mirror=True)

    kinds = {a.kind for a in outcome.plan.actions}
    assert not kinds & {PhotoActionKind.DELETE, PhotoActionKind.GONE, PhotoActionKind.UNLINK}
    assert (tmp_path / "2026/09/CLIP.mov").exists()
    assert (tmp_path / "Alben/Urlaub/CLIP.mov").exists()


def test_archive_preview_deletes_nothing(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    preview = FileRule("alt", RuleAction.ARCHIVE, older_than_days=3 * 365)
    source.add("a1", "IMG_1.HEIC", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, preview, clock=clock)
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, preview, clock=clock)

    assert [(h.path, h.pending) for h in outcome.plan.rule_hits] == [
        ("2019/05/IMG_1.HEIC", "preview")
    ]
    assert source.deleted == []


def test_first_run_only_backs_up(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", created=OLD)

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD)

    assert outcome.plan.rule_hits[0].pending == "first run"
    assert source.deleted == []
    assert (tmp_path / "2019/05/IMG_1.HEIC").exists()


def test_archive_deletes_the_whole_photo_in_icloud_and_keeps_it_on_the_nas(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"still", created=OLD, albums=("Urlaub",))
    source.add("a1", "IMG_1.MOV", b"live", created=OLD, resource="original_video")
    source.add("a2", "IMG_2.HEIC", created=NEW)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, mirror=True)
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, mirror=True)

    assert outcome.result is not None and outcome.result.ok
    assert outcome.plan.counts() == {PhotoActionKind.ARCHIVE: 1}
    assert source.deleted == ["a1"]
    assert (tmp_path / "2019/05/IMG_1.HEIC").read_bytes() == b"still"
    assert (tmp_path / "2019/05/IMG_1.MOV").read_bytes() == b"live"
    records = pstate.records("photos")
    assert all(r.archived_at for k, r in records.items() if "/a1/" in k)
    assert records["personal/a2/original"].archived_at is None

    clock.next_day()
    later = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, mirror=True)

    assert later.plan.actions == []  # not gone, not deleted, links kept
    assert (tmp_path / "2019/05/IMG_1.HEIC").exists()
    assert (tmp_path / "Alben/Urlaub/IMG_1.HEIC").exists()


def test_archive_keeps_the_photo_in_icloud_when_the_nas_copy_differs(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"pixels", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    (tmp_path / "2019/05/IMG_1.HEIC").write_bytes(b"pixelz")  # bit rot, same size
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)

    assert outcome.result is not None
    assert "differs from iCloud" in outcome.result.failed[0].error
    assert source.deleted == []
    assert pstate.records("photos")["personal/a1/original"].archived_at is None


def test_archive_limit_holds_until_approved(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    for i in range(3):
        source.add(f"a{i}", f"IMG_{i}.HEIC", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    clock.next_day()

    held = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, limit=2)

    assert held.result is not None and held.result.blocked
    assert source.deleted == []

    clock.next_day()
    approved = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, limit=2, allow=True)

    assert approved.result is not None and not approved.result.blocked
    assert sorted(source.deleted) == ["a0", "a1", "a2"]


def test_dry_run_archives_nothing(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, dry_run=True)

    assert outcome.plan.archives == 1
    assert source.deleted == []


@pytest.mark.parametrize(
    ("library", "can_delete", "pending"),
    [(Library.SHARED, True, "shared library"), (Library.PERSONAL, False, "not supported")],
)
def test_archive_never_touches_the_shared_library_or_sources_without_delete(
    library: Library,
    can_delete: bool,
    pending: str,
    source: FakePhotoSource,
    pstate: PhotoStateDB,
    tmp_path: Path,
) -> None:
    source.add("a1", "IMG_1.HEIC", created=OLD, library=library)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, can_delete=can_delete)
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock, can_delete=can_delete)

    assert outcome.plan.rule_hits[0].pending == pending
    assert source.deleted == []


def test_photo_with_a_missing_file_waits(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    (tmp_path / "2019/05/IMG_1.HEIC").unlink()
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)

    assert outcome.plan.counts() == {PhotoActionKind.RESTORE: 1}
    assert outcome.plan.rule_hits[0].pending == "needs another sync"
    assert source.deleted == []


def test_restored_archived_photo_is_backed_up_again(
    source: FakePhotoSource, pstate: PhotoStateDB, tmp_path: Path
) -> None:
    source.add("a1", "IMG_1.HEIC", b"x", created=OLD)
    clock = Clock()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    clock.next_day()
    backup(source, tmp_path, pstate, ARCHIVE_OLD, clock=clock)
    source.add("a1", "IMG_1.HEIC", b"x", created=OLD)  # restored from Recently Deleted
    clock.next_day()

    outcome = backup(source, tmp_path, pstate, clock=clock)

    assert outcome.plan.counts() == {PhotoActionKind.RETURNED: 1}
    assert pstate.records("photos")["personal/a1/original"].archived_at is None
