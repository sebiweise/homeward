"""First-run merge, delete brake, dry run, exclusions and partial failures."""

from datetime import date
from pathlib import Path

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, FolderMissing, SyncMode
from homeward.core.planner import DeleteBrake
from homeward.core.state import StateDB
from homeward.core.sync import sync_pair
from homeward.providers.fake import FakeProvider
from homeward.providers.nas import NasProvider

TODAY = date(2026, 10, 11)


def _pair(mode: SyncMode = SyncMode.BIDIRECTIONAL, exclude: tuple[str, ...] = ()) -> PairConfig:
    return PairConfig(name="p", local=Path("/x"), remote="r", mode=mode, exclude=exclude)


def _synced(state: StateDB, nas: FakeProvider, icloud: FakeProvider, count: int) -> None:
    for i in range(count):
        state.record("p", nas.put(f"{i}.txt", str(i)), icloud.put(f"{i}.txt", str(i)))


def test_first_run_merges_without_deleting(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    nas.put("only-nas.txt", "a")
    nas.put("both.txt", "nas")
    icloud.put("both.txt", "icloud")
    icloud.put("only-icloud.txt", "c")
    icloud.put("same.txt", "s")
    nas.put("same.txt", "s")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False, today=TODAY)

    assert outcome.plan.first_run
    assert outcome.plan.deletes == 0
    assert set(nas.files) == set(icloud.files)
    assert nas.content("both (Konflikt NAS 2026-10-11).txt") == b"nas"
    assert nas.content("both.txt") == icloud.content("both.txt") == b"icloud"
    assert nas.trash == []
    assert icloud.trash == []
    assert set(state.records("p")) == set(nas.files)


def test_first_run_one_way_never_overwrites(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    nas.put("a.txt", "nas")
    icloud.put("a.txt", "icloud")
    icloud.put("b.txt", "x")
    nas.put("b.txt", "x")

    first = sync_pair(_pair(SyncMode.UPLOAD), nas, icloud, state, dry_run=False)

    assert [a.kind for a in first.plan.actions] == [ActionKind.SKIP, ActionKind.RECORD]
    assert icloud.content("a.txt") == b"icloud"
    # The next run is no longer a first run: NAS wins, the old iCloud copy is trashed.
    second = sync_pair(_pair(SyncMode.UPLOAD), nas, icloud, state, dry_run=False)
    assert not second.plan.first_run
    assert icloud.content("a.txt") == b"nas"
    assert icloud.trashed("a.txt") == [b"icloud"]


def test_first_run_records_same_content_without_hashes(state: StateDB) -> None:
    nas, icloud = (
        FakeProvider("nas", known_hashes=False),
        FakeProvider("icloud", known_hashes=False),
    )
    nas.put("a.txt", "same")
    icloud.put("a.txt", "same")

    outcome = sync_pair(_pair(SyncMode.DOWNLOAD), nas, icloud, state, dry_run=False)

    assert outcome.plan.actions[0].kind is ActionKind.SKIP
    assert outcome.plan.actions[0].verify_content
    assert "a.txt" in state.records("p")


def test_delete_brake_by_count(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, 1000)
    for i in range(51):
        nas.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked == "51 deletions exceed the limit of 50 files"
    assert outcome.result is not None
    assert outcome.result.blocked
    assert len(icloud.files) == 1000
    assert icloud.trash == []


def test_delete_brake_by_ratio(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, 20)
    for i in range(6):
        icloud.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked == "6 of 20 files would be deleted, more than 10%"
    assert len(nas.files) == 20


def test_delete_brake_can_be_approved(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 20)
    for i in range(6):
        icloud.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False, allow_deletes=True)

    assert outcome.result is not None
    assert outcome.result.ok
    assert len(nas.files) == 14
    assert len(nas.trash) == 6


def test_delete_brake_allows_small_deletions(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 100)
    for i in range(10):
        nas.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked is None
    assert len(icloud.files) == 90


def test_delete_brake_ratio_starts_above_five_deletions() -> None:
    brake = DeleteBrake()
    assert brake.check(5, 6) is None  # a few files in a small folder
    assert brake.check(6, 20) == "6 of 20 files would be deleted, more than 10%"
    assert brake.check(6, 60) is None


def test_an_emptied_side_is_held_back_by_the_brake(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 6)
    for i in range(6):
        nas.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked == "6 of 6 files would be deleted, more than 10%"
    assert len(icloud.files) == 6


def test_a_missing_folder_stops_the_run_before_anything_changes(
    state: StateDB, tmp_path: Path, icloud: FakeProvider
) -> None:
    root = tmp_path / "share" / "Docs"
    root.mkdir(parents=True)
    nas = NasProvider(root)
    (root / "a.txt").write_text("a")
    state.record("p", nas.list_files()["a.txt"], icloud.put("a.txt", "a"))
    (root / "a.txt").unlink()
    root.rmdir()  # e.g. the shared folder is not mounted

    with pytest.raises(FolderMissing, match=r"NAS folder .* is missing, but 1 files"):
        sync_pair(_pair(SyncMode.UPLOAD), nas, icloud, state, dry_run=False)

    assert icloud.files.keys() == {"a.txt"}
    assert icloud.trash == []


def test_a_missing_folder_on_the_first_run_is_fine(
    state: StateDB, tmp_path: Path, nas: FakeProvider
) -> None:
    remote = NasProvider(tmp_path / "not-yet")
    nas.put("a.txt", "a")

    outcome = sync_pair(_pair(SyncMode.UPLOAD), nas, remote, state, dry_run=False)

    assert outcome.result is not None and outcome.result.ok
    assert (tmp_path / "not-yet" / "a.txt").read_text() == "a"


def test_delete_brake_is_configurable() -> None:
    brake = DeleteBrake(max_ratio=0.5, max_count=5)
    assert brake.check(5, 10) is None
    assert brake.check(6, 100) is not None
    assert brake.check(6, 10) is not None


def test_dry_run_changes_nothing(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, 30)
    nas.put("new.txt", "n")
    icloud.remove("0.txt")
    records = state.records("p")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=True)

    assert outcome.result is None
    assert {a.kind for a in outcome.plan.actions} == {ActionKind.UPLOAD, ActionKind.DELETE_LOCAL}
    assert "new.txt" not in icloud.files
    assert "0.txt" in nas.files
    assert state.records("p") == records


def test_exclusions(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, 30)
    nas.put("build/out.tmp", "x")
    nas.put("@eaDir/thumb.jpg", "x")
    nas.put(".DS_Store", "x")
    # A file excluded after it was synced is left alone on both sides.
    state.record("p", nas.put("cache/a.bin", "a"), icloud.put("cache/a.bin", "a"))
    nas.remove("cache/a.bin")

    outcome = sync_pair(_pair(exclude=("*.tmp", "cache")), nas, icloud, state, dry_run=False)

    assert outcome.plan.actions == []
    assert "cache/a.bin" in icloud.files


def test_failed_step_keeps_state_and_continues(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 30)
    nas.put("0.txt", "edit")
    nas.put("1.txt", "edit")
    icloud.fail_paths.add("0.txt")
    before = state.records("p")["0.txt"]

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.result is not None
    assert [f.action.path for f in outcome.result.failed] == ["0.txt"]
    assert icloud.content("1.txt") == b"edit"
    assert state.records("p")["0.txt"] == before
    icloud.fail_paths.clear()
    retry = sync_pair(_pair(), nas, icloud, state, dry_run=False)
    assert [a.path for a in retry.plan.actions] == ["0.txt"]
    assert icloud.content("0.txt") == b"edit"


def test_conflict_names_do_not_collide(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 30)
    state.record("p", nas.put("f.txt", "base"), icloud.put("f.txt", "base"))
    taken = "f (Konflikt NAS 2026-10-11).txt"
    state.record("p", nas.put(taken, "old"), icloud.put(taken, "old"))
    nas.put("f.txt", "nas")
    icloud.put("f.txt", "icloud")

    sync_pair(_pair(), nas, icloud, state, dry_run=False, today=TODAY)

    assert nas.content(taken) == b"old"
    assert nas.content("f (Konflikt NAS 2026-10-11 2).txt") == b"nas"
    assert icloud.content("f (Konflikt NAS 2026-10-11 2).txt") == b"nas"


def test_interrupted_overwrite_is_copied_again_not_deleted(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 30)
    nas.put("0.txt", "edit")
    write = icloud.write

    def fail_write(*args: object, **kwargs: object) -> object:
        raise OSError("cancelled mid-upload")

    icloud.write = fail_write  # type: ignore[method-assign]
    sync_pair(_pair(), nas, icloud, state, dry_run=False)
    # The old iCloud version went to the trash, the new one never arrived.
    assert "0.txt" not in icloud.files and icloud.trashed("0.txt") == [b"0"]
    assert "0.txt" not in state.records("p")

    icloud.write = write  # type: ignore[method-assign]
    retry = sync_pair(_pair(), nas, icloud, state, dry_run=False)
    # A missing file without a record is copied again, never read as a delete.
    assert [(a.kind, a.path) for a in retry.plan.actions] == [(ActionKind.UPLOAD, "0.txt")]
    assert nas.content("0.txt") == icloud.content("0.txt") == b"edit"
