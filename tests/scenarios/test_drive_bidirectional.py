"""Bidirectional mode end to end: a real NAS folder <-> iCloud Drive (fake server)."""

from datetime import date
from pathlib import Path

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, SyncMode
from homeward.core.rules import TRASH_DIR
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.providers.fake_drive import FakeDriveApi
from homeward.providers.icloud_drive import FolderCache, ICloudDriveProvider
from homeward.providers.nas import NasProvider

TODAY = date(2026, 10, 11)


class Pair:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "share" / "Docs"
        self.root.mkdir(parents=True)
        self.api = FakeDriveApi()
        self.config = PairConfig(
            name="docs", local=self.root, remote="Docs", mode=SyncMode.BIDIRECTIONAL, beta=True
        )
        self.state = StateDB(":memory:")
        self.cache = FolderCache(tmp_path / "cache.json")

    def icloud(self) -> ICloudDriveProvider:
        return ICloudDriveProvider(self.api, "Docs", cache=self.cache)

    def run(self, *, dry_run: bool = False) -> SyncOutcome:
        nas = NasProvider(self.root, today=lambda: TODAY)
        outcome = sync_pair(
            self.config, nas, self.icloud(), self.state, dry_run=dry_run, today=TODAY
        )
        if not dry_run:
            assert outcome.result is not None and outcome.result.ok, outcome.result
        return outcome

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def read(self, path: str) -> str:
        return (self.root / path).read_text()

    def nas_tree(self) -> dict[str, bytes]:
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and TRASH_DIR not in p.parts
        }

    def icloud_tree(self) -> dict[str, bytes]:
        icloud = self.icloud()
        return {path: self.api.content(f"Docs/{path}") for path in icloud.list_files()}

    def nas_trash(self) -> dict[str, str]:
        day = self.root / TRASH_DIR / TODAY.isoformat()
        return {p.relative_to(day).as_posix(): p.read_text() for p in day.rglob("*") if p.is_file()}

    def assert_in_sync(self) -> None:
        assert self.nas_tree() == self.icloud_tree()
        assert self.run(dry_run=True).plan.actions == []


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    p = Pair(tmp_path)
    for i in range(20):
        p.write(f"keep/{i}.txt", str(i))
    p.write("report.txt", "v1")
    p.write("Fotos/a.jpg", "a")
    p.run()
    p.assert_in_sync()
    return p


def _kinds(outcome: SyncOutcome) -> dict[str, ActionKind]:
    return {action.path: action.kind for action in outcome.plan.actions}


def test_changes_on_both_sides(pair: Pair) -> None:
    pair.write("report.txt", "v2 from the NAS")
    pair.write("new-on-nas.txt", "n")
    pair.api.put("Docs/Fotos/a.jpg", "a edited on the iPhone")
    pair.api.put("Docs/new-in-icloud.txt", "i")

    assert _kinds(pair.run()) == {
        "report.txt": ActionKind.UPLOAD,
        "new-on-nas.txt": ActionKind.UPLOAD,
        "Fotos/a.jpg": ActionKind.DOWNLOAD,
        "new-in-icloud.txt": ActionKind.DOWNLOAD,
    }
    assert pair.read("Fotos/a.jpg") == "a edited on the iPhone"
    assert ("report.txt", b"v1") in pair.api.trashed
    assert pair.nas_trash() == {"Fotos/a.jpg": "a"}
    pair.assert_in_sync()


def test_conflict_keeps_both(pair: Pair) -> None:
    pair.write("report.txt", "NAS edit")
    pair.api.put("Docs/report.txt", "iCloud edit")

    assert _kinds(pair.run()) == {"report.txt": ActionKind.CONFLICT}
    assert pair.read("report.txt") == "iCloud edit"
    copy = "report (Konflikt NAS 2026-10-11).txt"
    assert pair.read(copy) == "NAS edit"
    assert pair.api.content(f"Docs/{copy}") == b"NAS edit"
    pair.assert_in_sync()


def test_delete_on_either_side(pair: Pair) -> None:
    (pair.root / "keep" / "0.txt").unlink()
    pair.api.remove("Docs/keep/1.txt")

    pair.run()

    assert ("0.txt", b"0") in pair.api.trashed
    assert pair.nas_trash() == {"keep/1.txt": "1"}
    pair.assert_in_sync()


def test_rename_on_the_nas(pair: Pair) -> None:
    (pair.root / "report.txt").rename(pair.root / "Bericht.txt")

    outcome = pair.run()

    assert outcome.plan.renames == 1 and outcome.plan.deletes == 0
    assert pair.api.content("Docs/Bericht.txt") == b"v1"
    assert ("report.txt", b"v1") in pair.api.trashed
    pair.assert_in_sync()


def test_rename_and_move_in_icloud(pair: Pair) -> None:
    icloud = pair.icloud()
    icloud.list_files()
    icloud.move("report.txt", "Archiv/report-2026.txt")

    outcome = pair.run()

    # iCloud gives the moved file a new etag but keeps its docwsid.
    assert outcome.plan.renames == 1 and outcome.plan.blocked is None
    assert pair.read("Archiv/report-2026.txt") == "v1"
    assert pair.nas_trash() == {"report.txt": "v1"}
    pair.assert_in_sync()


def test_folder_rename_in_icloud_passes_the_brake(pair: Pair) -> None:
    icloud = pair.icloud()
    icloud.list_files()
    for i in range(20):
        icloud.move(f"keep/{i}.txt", f"kept/{i}.txt")

    outcome = pair.run()

    assert outcome.plan.renames == 20 and outcome.plan.blocked is None
    assert pair.read("kept/7.txt") == "7"
    assert not (pair.root / "keep" / "7.txt").exists()
    pair.assert_in_sync()


def test_case_only_rename_on_the_nas(pair: Pair) -> None:
    (pair.root / "report.txt").rename(pair.root / "Report.txt")

    outcome = pair.run()

    assert outcome.plan.actions[0].kind is ActionKind.DELETE_REMOTE
    assert pair.api.content("Docs/Report.txt") == b"v1"
    assert not pair.api.exists("Docs/report.txt")
    pair.assert_in_sync()


def test_names_that_differ_only_in_case_stay_on_the_nas(pair: Pair) -> None:
    pair.write("fotos/b.jpg", "b")

    outcome = pair.run()

    assert _kinds(outcome) == {"fotos/b.jpg": ActionKind.SKIP}
    assert not pair.api.exists("Docs/fotos")
    assert pair.read("fotos/b.jpg") == "b"


def test_dry_run_changes_nothing(pair: Pair) -> None:
    pair.write("report.txt", "v2")
    pair.api.remove("Docs/Fotos/a.jpg")
    before = (pair.nas_tree(), pair.icloud_tree())

    outcome = pair.run(dry_run=True)

    assert _kinds(outcome) == {
        "report.txt": ActionKind.UPLOAD,
        "Fotos/a.jpg": ActionKind.DELETE_LOCAL,
    }
    assert (pair.nas_tree(), pair.icloud_tree()) == before
