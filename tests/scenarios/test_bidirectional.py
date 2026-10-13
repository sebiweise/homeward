"""End-to-end bidirectional runs: renames, case collisions, conflicts over several
runs, and a simulated week of random edits on both sides."""

import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, SyncMode
from homeward.core.planner import DeleteBrake
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.providers.fake import FakeProvider

TODAY = date(2026, 10, 11)


def _pair(brake: DeleteBrake | None = None) -> PairConfig:
    return PairConfig(
        name="p",
        local=Path("/x"),
        remote="r",
        mode=SyncMode.BIDIRECTIONAL,
        beta=True,
        delete_brake=brake or DeleteBrake(),
    )


def _synced(state: StateDB, nas: FakeProvider, icloud: FakeProvider, paths: list[str]) -> None:
    for path in paths:
        state.record("p", nas.put(path, path), icloud.put(path, path))


def _run(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> SyncOutcome:
    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False, today=TODAY)
    assert outcome.result is not None
    return outcome


def _tree(side: FakeProvider) -> dict[str, bytes]:
    return {path: entry.data for path, entry in side.files.items()}


def _filler(count: int) -> list[str]:
    return [f"keep/{i}.txt" for i in range(count)]


@pytest.fixture
def icloud() -> FakeProvider:
    return FakeProvider("icloud", case_insensitive=True)


# Renames and moves


def test_nas_rename_is_delete_plus_new(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["old.txt", *_filler(5)])
    nas.move("old.txt", "new.txt")

    outcome = _run(state, nas, icloud)

    assert outcome.result.ok
    assert outcome.plan.renames == 1
    assert outcome.plan.deletes == 0
    assert outcome.plan.blocked is None  # 1 of 6 files would otherwise trip the 10 % brake
    assert _tree(icloud) == _tree(nas)
    assert icloud.trashed("old.txt") == [b"old.txt"]
    assert "old.txt" not in state.records("p")


def test_icloud_move_into_folder(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, ["a.txt", *_filler(5)])
    icloud.move("a.txt", "archive/a.txt")

    outcome = _run(state, nas, icloud)

    assert outcome.plan.renames == 1 and outcome.plan.blocked is None
    assert _tree(nas) == _tree(icloud)
    assert nas.trashed("a.txt") == [b"a.txt"]


def test_folder_rename_passes_the_delete_brake(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    photos = [f"Fotos/{i}.jpg" for i in range(60)]
    _synced(state, nas, icloud, [*photos, *_filler(40)])
    for path in photos:
        nas.move(path, path.replace("Fotos/", "Bilder/"))

    outcome = _run(state, nas, icloud)

    assert outcome.plan.renames == 60
    assert outcome.plan.blocked is None
    assert outcome.result.ok
    assert _tree(icloud) == _tree(nas)
    assert len(icloud.trash) == 60


def test_real_deletes_next_to_renames_still_trip_the_brake(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, [f"{i}.txt" for i in range(100)])
    nas.move("0.txt", "zero.txt")
    for i in range(1, 12):
        nas.remove(f"{i}.txt")

    outcome = _run(state, nas, icloud)

    assert outcome.plan.renames == 1
    assert outcome.plan.blocked == "11 of 100 files would be deleted, more than 10%"
    assert icloud.trash == []


def test_rename_with_edit_counts_as_delete(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["a.txt", *_filler(20)])
    nas.remove("a.txt")
    nas.put("b.txt", "a.txt but edited")

    outcome = _run(state, nas, icloud)

    assert outcome.plan.renames == 0 and outcome.plan.deletes == 1
    assert _tree(icloud) == _tree(nas)


def test_rename_detected_by_checksum_when_version_changes(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    # A copy plus delete gives the new file a new version token but the same content.
    _synced(state, nas, icloud, ["a.txt", *_filler(5)])
    nas.remove("a.txt")
    nas.put("b.txt", "a.txt")

    assert _run(state, nas, icloud).plan.renames == 1


def test_renamed_file_keeps_old_copy_when_transfer_fails(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["old.txt", *_filler(5)])
    nas.move("old.txt", "new.txt")
    icloud.fail_paths.add("new.txt")

    outcome = _run(state, nas, icloud)

    assert [f.action.path for f in outcome.result.failed] == ["new.txt"]
    assert icloud.content("old.txt") == b"old.txt"  # not trashed while new.txt is missing
    icloud.fail_paths.clear()
    assert _run(state, nas, icloud).result.ok
    assert _tree(icloud) == _tree(nas)


def test_rename_on_nas_while_icloud_edits_the_old_name(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["a.txt", *_filler(5)])
    nas.move("a.txt", "b.txt")
    icloud.put("a.txt", "edited in iCloud")

    outcome = _run(state, nas, icloud)

    assert outcome.result.ok
    assert nas.content("a.txt") == b"edited in iCloud"  # the edit wins and is restored
    assert icloud.content("b.txt") == b"a.txt"
    assert _tree(icloud) == _tree(nas)


def test_deletes_run_after_transfers(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["a.txt", "z.txt", *_filler(30)])
    nas.remove("a.txt")
    nas.put("m.txt", "m")
    icloud.put("z.txt", "z2")

    kinds = [a.kind for a in _run(state, nas, icloud).plan.actions]

    assert kinds == [ActionKind.UPLOAD, ActionKind.DOWNLOAD, ActionKind.DELETE_REMOTE]


# Upper/lower case (iCloud Drive does not keep such names apart)


def test_case_only_rename(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, ["readme.txt", *_filler(5)])
    nas.move("readme.txt", "README.txt")

    outcome = _run(state, nas, icloud)

    assert outcome.result.ok, outcome.result.failed
    assert outcome.plan.actions[0].kind is ActionKind.DELETE_REMOTE  # frees the name first
    assert _tree(icloud) == _tree(nas)
    assert icloud.trashed("readme.txt") == [b"readme.txt"]


def test_names_differing_only_in_case_are_skipped(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["Notes.txt", *_filler(5)])
    nas.put("notes.txt", "second file")
    nas.put("Docs/a.txt", "a")
    nas.put("docs/b.txt", "b")

    outcome = _run(state, nas, icloud)

    skipped = {a.path: a.reason for a in outcome.plan.actions if a.kind is ActionKind.SKIP}
    assert set(skipped) == {"notes.txt", "docs/b.txt"}
    assert "Notes.txt" in skipped["notes.txt"]
    assert "Docs" in skipped["docs/b.txt"]
    assert outcome.result.ok
    assert icloud.content("Notes.txt") == b"Notes.txt"
    assert "Docs/a.txt" in icloud.files
    assert nas.content("notes.txt") == b"second file"  # stays on the NAS, nothing lost


def test_case_guard_only_for_case_insensitive_remotes(state: StateDB, nas: FakeProvider) -> None:
    icloud = FakeProvider("folder")
    _synced(state, nas, icloud, ["Notes.txt", *_filler(5)])
    nas.put("notes.txt", "second file")

    assert _run(state, nas, icloud).result.ok
    assert icloud.content("notes.txt") == b"second file"


# Conflicts over several runs


def test_conflict_settles_and_copies_sync_like_any_file(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["f.txt", *_filler(20)])
    nas.put("f.txt", "nas")
    icloud.put("f.txt", "icloud")
    copy = "f (Konflikt NAS 2026-10-11).txt"

    first = _run(state, nas, icloud)
    assert [a.kind for a in first.plan.actions] == [ActionKind.CONFLICT]
    assert _run(state, nas, icloud).plan.actions == []

    # The user merges by hand in iCloud and deletes the conflict copy on the NAS.
    icloud.put("f.txt", "merged")
    nas.remove(copy)
    third = _run(state, nas, icloud)

    assert third.result.ok
    assert nas.content("f.txt") == b"merged"
    assert copy not in icloud.files
    assert icloud.trashed(copy) == [b"nas"]
    assert _tree(nas) == _tree(icloud)


def test_second_conflict_on_the_same_day_gets_a_number(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, ["f.txt", *_filler(5)])
    for round_ in ("1", "2"):
        nas.put("f.txt", f"nas {round_}")
        icloud.put("f.txt", f"icloud {round_}")
        assert _run(state, nas, icloud).result.ok

    assert nas.content("f (Konflikt NAS 2026-10-11).txt") == b"nas 1"
    assert nas.content("f (Konflikt NAS 2026-10-11 2).txt") == b"nas 2"
    assert _tree(nas) == _tree(icloud)


# A simulated week


DAYS, RUNS_PER_DAY = 7, 24
NAMES = [f"{folder}/{i}.txt" for folder in ("docs", "fotos", "docs/alt") for i in range(6)]


def _random_edits(rng: random.Random, side: FakeProvider, tag: str) -> None:
    for _ in range(rng.randint(0, 3)):
        existing = sorted(side.files)
        free = [name for name in NAMES if name not in side.files]
        op = rng.choice(["edit", "new", "delete", "rename"])
        if op == "new" and free:
            side.put(rng.choice(free), f"{tag}-{rng.random()}")
        elif op == "edit" and existing:
            side.put(rng.choice(existing), f"{tag}-{rng.random()}")
        elif op == "delete" and existing:
            side.remove(rng.choice(existing))
        elif op == "rename" and existing and free:
            side.move(rng.choice(existing), rng.choice(free))


def _everything(*sides: FakeProvider) -> set[bytes]:
    return {data for side in sides for data in [*_tree(side).values(), *(d for _, d in side.trash)]}


@pytest.mark.parametrize("seed", range(10))
def test_a_week_of_random_edits_on_both_sides(seed: int) -> None:
    rng = random.Random(seed)
    nas, icloud = FakeProvider("nas"), FakeProvider("icloud", case_insensitive=True)
    state = StateDB(":memory:")
    pair = _pair(DeleteBrake(max_ratio=1.0, max_count=10_000))

    for run in range(DAYS * RUNS_PER_DAY):
        _random_edits(rng, nas, f"nas{run}")
        _random_edits(rng, icloud, f"icloud{run}")
        before = _everything(nas, icloud)
        today = TODAY + timedelta(days=run // RUNS_PER_DAY)

        outcome = sync_pair(pair, nas, icloud, state, dry_run=False, today=today)

        assert outcome.result is not None and outcome.result.ok, (run, outcome.result)
        assert before <= _everything(nas, icloud), f"content lost in run {run}"
        if outcome.plan.first_run:
            continue
        assert _tree(nas) == _tree(icloud), f"sides differ after run {run}"
        again = sync_pair(pair, nas, icloud, state, dry_run=True, today=today)
        assert again.plan.actions == [], f"run {run} did not converge"
