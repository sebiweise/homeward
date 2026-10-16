"""Upload and download mode end to end: NAS folder <-> iCloud Drive (fake server)."""

from datetime import date
from pathlib import Path

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, SyncMode
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.providers.fake_drive import FakeDriveApi
from homeward.providers.icloud_drive import FolderCache, ICloudDriveProvider
from homeward.providers.nas import NasProvider

TODAY = date(2026, 10, 11)


class Pair:
    def __init__(self, tmp_path: Path, mode: SyncMode, recycle: bool = False) -> None:
        self.root = tmp_path / "share" / "Docs"
        self.root.mkdir(parents=True)
        self.recycle = tmp_path / "share" / "#recycle"
        if recycle:
            self.recycle.mkdir()
        self.api = FakeDriveApi()
        self.config = PairConfig(
            name="docs", local=self.root, remote="Docs", mode=mode, exclude=("*.bak",)
        )
        self.state = StateDB(":memory:")
        self.cache = FolderCache(tmp_path / "cache.json")

    def run(self, **kwargs: bool) -> SyncOutcome:
        nas = NasProvider(
            self.root, recycle_bin=self.recycle if self.recycle.is_dir() else None,
            today=lambda: TODAY,
        )  # fmt: skip
        icloud = ICloudDriveProvider(
            self.api, "Docs", excludes=self.config.exclude, cache=self.cache
        )
        outcome = sync_pair(self.config, nas, icloud, self.state, dry_run=False, **kwargs)
        assert outcome.result is not None
        assert outcome.result.ok, outcome.result
        return outcome

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _kinds(outcome: SyncOutcome) -> dict[str, ActionKind]:
    return {action.path: action.kind for action in outcome.plan.actions}


@pytest.fixture
def up(tmp_path: Path) -> Pair:
    return Pair(tmp_path, SyncMode.UPLOAD)


@pytest.fixture
def down(tmp_path: Path) -> Pair:
    return Pair(tmp_path, SyncMode.DOWNLOAD, recycle=True)


def test_upload_mode(up: Pair) -> None:
    up.write("a.txt", "a")
    up.write("sub/b.txt", "b")
    up.write("skip.bak", "x")
    up.write(".DS_Store", "x")
    up.api.put("Docs/only-in-icloud.txt", "i")

    assert _kinds(up.run()) == {"a.txt": ActionKind.UPLOAD, "sub/b.txt": ActionKind.UPLOAD}
    assert up.api.content("Docs/sub/b.txt") == b"b"
    assert not up.api.exists("Docs/skip.bak")
    assert not up.api.exists("Docs/.DS_Store")
    assert up.run().plan.actions == []  # etags are stable: nothing to do

    up.write("a.txt", "a2")
    (up.root / "sub" / "b.txt").unlink()
    # Deleting 1 of 2 tracked files trips the delete brake, so approve it.
    assert _kinds(up.run(allow_deletes=True)) == {
        "a.txt": ActionKind.UPLOAD,
        "sub/b.txt": ActionKind.DELETE_REMOTE,
    }
    assert up.api.content("Docs/a.txt") == b"a2"
    assert sorted(up.api.trashed) == [("a.txt", b"a"), ("b.txt", b"b")]  # old versions kept
    assert up.api.content("Docs/only-in-icloud.txt") == b"i"  # target-only files stay

    up.api.put("Docs/a.txt", "edited in iCloud")
    assert _kinds(up.run()) == {"a.txt": ActionKind.UPLOAD}  # the NAS wins
    assert up.api.content("Docs/a.txt") == b"a2"
    assert ("a.txt", b"edited in iCloud") in up.api.trashed


def test_download_mode(down: Pair) -> None:
    down.api.put("Docs/a.txt", "a", mtime=1_700_000_000.0)
    down.api.put("Docs/deep/er/b.txt", "b")
    down.api.put("Docs/empty.txt", "")
    down.write("only-on-nas.txt", "n")

    assert _kinds(down.run()) == {
        "a.txt": ActionKind.DOWNLOAD,
        "deep/er/b.txt": ActionKind.DOWNLOAD,
        "empty.txt": ActionKind.DOWNLOAD,
    }
    assert (down.root / "deep" / "er" / "b.txt").read_text() == "b"
    assert (down.root / "a.txt").stat().st_mtime == 1_700_000_000.0
    assert (down.root / "empty.txt").read_bytes() == b""
    assert down.run().plan.actions == []

    down.api.put("Docs/a.txt", "a2")
    down.api.remove("Docs/deep/er/b.txt")
    assert _kinds(down.run(allow_deletes=True)) == {
        "a.txt": ActionKind.DOWNLOAD,
        "deep/er/b.txt": ActionKind.DELETE_LOCAL,
    }
    assert (down.root / "a.txt").read_text() == "a2"
    # Old versions land in the share's recycle bin, with DSM's layout.
    assert (down.recycle / "Docs" / "a.txt").read_text() == "a"
    assert (down.recycle / "Docs" / "deep" / "er" / "b.txt").read_text() == "b"
    assert (down.root / "only-on-nas.txt").read_text() == "n"


def test_first_run_with_existing_files_on_both_sides(down: Pair) -> None:
    down.api.put("Docs/same.txt", "same")
    down.api.put("Docs/differs.txt", "icloud")
    down.write("same.txt", "same")
    down.write("differs.txt", "nas")
    kinds = _kinds(down.run())
    assert kinds["same.txt"] in (ActionKind.RECORD, ActionKind.SKIP)
    assert kinds["differs.txt"] == ActionKind.SKIP  # first run never overwrites
    assert (down.root / "differs.txt").read_text() == "nas"
    assert _kinds(down.run()) == {"differs.txt": ActionKind.DOWNLOAD}
    assert (down.root / "differs.txt").read_text() == "icloud"


def test_mass_delete_in_icloud_hits_the_brake(down: Pair) -> None:
    for i in range(10):
        down.api.put(f"Docs/{i}.txt", str(i))
    down.run()
    for i in range(6):
        down.api.remove(f"Docs/{i}.txt")
    nas = NasProvider(down.root)
    icloud = ICloudDriveProvider(down.api, "Docs")
    outcome = sync_pair(down.config, nas, icloud, down.state, dry_run=False)
    assert outcome.result is not None
    assert outcome.result.blocked
    assert len(list(down.root.glob("*.txt"))) == 10
