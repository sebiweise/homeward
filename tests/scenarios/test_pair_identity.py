"""A pair's sync state belongs to its folders and account (``PairConfig.identity``)."""

from pathlib import Path

from homeward.core.config import PairConfig
from homeward.core.models import SyncMode
from homeward.core.state import StateDB
from homeward.core.sync import sync_pair
from homeward.providers.fake import FakeProvider


def _pair(remote: str = "r") -> PairConfig:
    return PairConfig(name="p", local=Path("/x"), remote=remote, mode=SyncMode.UPLOAD)


def _synced(state: StateDB, nas: FakeProvider, icloud: FakeProvider, count: int = 3) -> None:
    for i in range(count):
        state.record("p", nas.put(f"{i}.txt", str(i)), icloud.put(f"{i}.txt", str(i)))


def test_state_from_before_identities_is_kept_and_gets_one(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud)
    assert state.identity("p") is None
    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)
    assert not outcome.plan.first_run
    assert state.identity("p") == _pair().identity


def test_other_folders_make_a_first_run(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud)
    state.set_identity("p", _pair("old").identity)
    nas.remove("0.txt")  # would be mirrored to iCloud if the old state counted
    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)
    assert outcome.plan.first_run and outcome.plan.deletes == 0
    assert "0.txt" in icloud.files
    assert state.identity("p") == _pair().identity


def test_a_run_still_going_with_the_old_folders_leaves_no_usable_state(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    old = _pair("old")
    state.set_identity("p", old.identity)
    state.reset("p")  # the UI changed the folders while the old run was going ...
    _synced(state, nas, icloud)  # ... and that run went on recording files
    nas.remove("0.txt")
    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=False)
    assert outcome.plan.first_run and outcome.plan.deletes == 0
    assert "0.txt" in icloud.files


def test_a_dry_run_shows_the_first_run_and_changes_nothing(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud)
    state.set_identity("p", _pair("old").identity)
    outcome = sync_pair(_pair(), nas, icloud, state, dry_run=True)
    assert outcome.plan.first_run
    assert len(state.records("p")) == 3
    assert state.identity("p") == _pair("old").identity


def test_reset_makes_the_next_run_a_first_run(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud)
    sync_pair(_pair(), nas, icloud, state, dry_run=False)
    assert state.reset("p") == 3
    assert state.identity("p") == ""
    assert sync_pair(_pair(), nas, icloud, state, dry_run=False).plan.first_run
