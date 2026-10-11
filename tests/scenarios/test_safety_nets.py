"""First-run merge, delete brake, dry run, exclusions and partial failures."""

from datetime import date
from pathlib import Path

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, SyncMode
from homeward.core.planner import DeleteBrake
from homeward.core.state import StateDB
from homeward.core.sync import sync_pair
from homeward.providers.fake import FakeProvider

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
    assert nas.trash == [] and icloud.trash == []
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
    assert outcome.result is not None and outcome.result.blocked
    assert len(icloud.files) == 1000
    assert icloud.trash == []


def test_delete_brake_by_ratio(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, 20)
    for i in range(3):
        icloud.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked == "3 of 20 files would be deleted, more than 10%"
    assert len(nas.files) == 20


def test_delete_brake_can_be_approved(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 20)
    for i in range(3):
        icloud.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False, allow_deletes=True)

    assert outcome.result is not None and outcome.result.ok
    assert len(nas.files) == 17
    assert len(nas.trash) == 3


def test_delete_brake_allows_small_deletions(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, 100)
    for i in range(10):
        nas.remove(f"{i}.txt")

    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)

    assert outcome.plan.blocked is None
    assert len(icloud.files) == 90


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
