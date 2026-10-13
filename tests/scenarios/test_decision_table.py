"""One scenario per row of every decision table (see CLAUDE.md).

Each scenario starts from a synced pair, applies the row's NAS and iCloud changes to
``f.txt``, runs a real sync against fake providers and checks both sides and both trashes.
Every scenario also checks two invariants: no content is lost, and a second run has
nothing left to do.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import Change, SyncMode
from homeward.core.rules import ROWS, TABLES
from homeward.core.state import StateDB
from homeward.core.sync import sync_pair
from homeward.providers.fake import FakeProvider

TODAY = date(2026, 10, 11)
PATH = "f.txt"
CONFLICT = "f (Konflikt NAS 2026-10-11).txt"
FILLER = 30  # untouched synced files, so one delete stays below the delete brake

U, C, D, N, A = Change.UNCHANGED, Change.CHANGED, Change.DELETED, Change.NEW, Change.ABSENT
BI, UP, DOWN = SyncMode.BIDIRECTIONAL, SyncMode.UPLOAD, SyncMode.DOWNLOAD


@dataclass(frozen=True)
class Expect:
    nas: str | None
    icloud: str | None
    nas_trash: tuple[str, ...] = ()
    icloud_trash: tuple[str, ...] = ()
    conflict: str | None = None  # content of the conflict copy on both sides


@dataclass(frozen=True)
class Case:
    mode: SyncMode
    row: tuple[Change, Change]
    expect: Expect
    same_content: bool = False  # both sides end up with identical new content
    hashes: bool = True
    label: str = field(default="")

    @property
    def id(self) -> str:
        suffix = f"-{self.label}" if self.label else ""
        return f"{self.mode}-{self.row[0]}-{self.row[1]}{suffix}"


CASES = [
    # Bidirectional
    Case(BI, (U, U), Expect("base", "base")),
    Case(BI, (C, U), Expect("nas", "nas", icloud_trash=("base",))),
    Case(BI, (U, C), Expect("icloud", "icloud", nas_trash=("base",))),
    Case(BI, (C, C), Expect("icloud", "icloud", conflict="nas"), label="differ"),
    Case(BI, (C, C), Expect("same", "same"), same_content=True, label="same"),
    Case(BI, (C, C), Expect("same", "same"), same_content=True, hashes=False, label="same-nohash"),
    Case(BI, (C, C), Expect("icloud", "icloud", conflict="nas"), hashes=False, label="nohash"),
    Case(BI, (D, U), Expect(None, None, icloud_trash=("base",))),
    Case(BI, (U, D), Expect(None, None, nas_trash=("base",))),
    Case(BI, (D, C), Expect("icloud", "icloud")),
    Case(BI, (C, D), Expect("nas", "nas")),
    Case(BI, (D, D), Expect(None, None)),
    Case(BI, (N, A), Expect("nas", "nas")),
    Case(BI, (A, N), Expect("icloud", "icloud")),
    Case(BI, (N, N), Expect("icloud", "icloud", conflict="nas"), label="differ"),
    Case(BI, (N, N), Expect("same", "same"), same_content=True, label="same"),
    # Upload: NAS wins
    Case(UP, (U, U), Expect("base", "base")),
    Case(UP, (C, U), Expect("nas", "nas", icloud_trash=("base",))),
    Case(UP, (U, C), Expect("base", "base", icloud_trash=("icloud",))),
    Case(UP, (C, C), Expect("nas", "nas", icloud_trash=("icloud",)), label="differ"),
    Case(UP, (C, C), Expect("same", "same"), same_content=True, label="same"),
    Case(UP, (D, U), Expect(None, None, icloud_trash=("base",))),
    Case(UP, (U, D), Expect("base", "base")),
    Case(UP, (D, C), Expect(None, None, icloud_trash=("icloud",))),
    Case(UP, (C, D), Expect("nas", "nas")),
    Case(UP, (D, D), Expect(None, None)),
    Case(UP, (N, A), Expect("nas", "nas")),
    Case(UP, (A, N), Expect(None, "icloud")),
    Case(UP, (N, N), Expect("nas", "nas", icloud_trash=("icloud",)), label="differ"),
    Case(UP, (N, N), Expect("same", "same"), same_content=True, label="same"),
    # Download: iCloud wins
    Case(DOWN, (U, U), Expect("base", "base")),
    Case(DOWN, (C, U), Expect("base", "base", nas_trash=("nas",))),
    Case(DOWN, (U, C), Expect("icloud", "icloud", nas_trash=("base",))),
    Case(DOWN, (C, C), Expect("icloud", "icloud", nas_trash=("nas",)), label="differ"),
    Case(DOWN, (C, C), Expect("same", "same"), same_content=True, label="same"),
    Case(DOWN, (D, U), Expect("base", "base")),
    Case(DOWN, (U, D), Expect(None, None, nas_trash=("base",))),
    Case(DOWN, (D, C), Expect("icloud", "icloud")),
    Case(DOWN, (C, D), Expect(None, None, nas_trash=("nas",))),
    Case(DOWN, (D, D), Expect(None, None)),
    Case(DOWN, (N, A), Expect("nas", None)),
    Case(DOWN, (A, N), Expect("icloud", "icloud")),
    Case(DOWN, (N, N), Expect("icloud", "icloud", nas_trash=("nas",)), label="differ"),
    Case(DOWN, (N, N), Expect("same", "same"), same_content=True, label="same"),
]


def _apply(side: FakeProvider, change: Change, content: str) -> None:
    if change in (Change.CHANGED, Change.NEW):
        side.put(PATH, content, mtime=1_800_000_000.0)
    elif change is Change.DELETED:
        side.remove(PATH)


def _contents(side: FakeProvider) -> list[bytes]:
    return [entry.data for entry in side.files.values()] + [data for _, data in side.trash]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_decision_table_row(case: Case) -> None:
    nas = FakeProvider("nas", known_hashes=case.hashes)
    icloud = FakeProvider("icloud", known_hashes=case.hashes)
    state = StateDB(":memory:")
    pair = PairConfig(name="p", local=Path("/x"), remote="r", mode=case.mode)

    for i in range(FILLER):
        state.record("p", nas.put(f"keep/{i}.txt", str(i)), icloud.put(f"keep/{i}.txt", str(i)))
    local_change, remote_change = case.row
    if Change.NEW not in case.row and Change.ABSENT not in case.row:
        state.record("p", nas.put(PATH, "base"), icloud.put(PATH, "base"))
    nas_new, icloud_new = ("same", "same") if case.same_content else ("nas", "icloud")
    _apply(nas, local_change, nas_new)
    _apply(icloud, remote_change, icloud_new)
    before = set(_contents(nas) + _contents(icloud))

    outcome = sync_pair(pair, nas, icloud, state, dry_run=False, today=TODAY)

    assert outcome.result is not None
    assert outcome.result.ok, outcome.result.failed
    expect = case.expect
    assert (nas.content(PATH).decode() if PATH in nas.files else None) == expect.nas
    assert (icloud.content(PATH).decode() if PATH in icloud.files else None) == expect.icloud
    assert tuple(d.decode() for d in nas.trashed(PATH)) == expect.nas_trash
    assert tuple(d.decode() for d in icloud.trashed(PATH)) == expect.icloud_trash
    if expect.conflict is None:
        assert CONFLICT not in nas.files
        assert CONFLICT not in icloud.files
    else:
        assert nas.content(CONFLICT).decode() == expect.conflict
        assert icloud.content(CONFLICT).decode() == expect.conflict

    # Never lose data: every content that existed before still exists somewhere.
    assert before <= set(_contents(nas) + _contents(icloud))
    # The run converged: planning again finds nothing to do.
    again = sync_pair(pair, nas, icloud, state, dry_run=True, today=TODAY)
    assert again.plan.actions == []


@pytest.mark.parametrize("mode", list(SyncMode))
def test_every_row_has_a_scenario(mode: SyncMode) -> None:
    assert set(TABLES[mode]) == set(ROWS)
    covered = {case.row for case in CASES if case.mode is mode}
    assert covered == set(ROWS)
