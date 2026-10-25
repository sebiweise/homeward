"""File rules on Drive pairs: ignore and archive (keep on the NAS, trash in iCloud)."""

from pathlib import Path
from typing import BinaryIO

import pytest

from homeward.core.config import PairConfig
from homeward.core.models import ActionKind, FileMeta, SyncMode
from homeward.core.state import StateDB
from homeward.core.sync import sync_pair
from homeward.core.workflow import DAY, FileRule, RuleAction
from homeward.providers.fake import FakeProvider

NOW = 1_800_000_000.0
OLD = NOW - 4 * 365 * DAY
RECENT = NOW - 10 * DAY

ARCHIVE_OLD = FileRule("alt", RuleAction.ARCHIVE, older_than_days=3 * 365, preview=False)
IGNORE_MP4 = FileRule("videos", RuleAction.IGNORE, extensions=("mp4",))


def _pair(
    *rules: FileRule, mode: SyncMode = SyncMode.BIDIRECTIONAL, archive_limit: int = 200
) -> PairConfig:
    return PairConfig(name="p", local=Path("/x"), remote="r", mode=mode,
                      rules=rules, archive_limit=archive_limit)  # fmt: skip


def _run(pair: PairConfig, nas: FakeProvider, icloud: FakeProvider, state: StateDB, **kwargs: bool):  # type: ignore[no-untyped-def]
    return sync_pair(pair, nas, icloud, state, dry_run=kwargs.pop("dry_run", False), **kwargs)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("homeward.core.planner.time.time", lambda: NOW)


def _synced(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider, path: str, mtime: float
) -> None:
    state.record("p", nas.put(path, path, mtime), icloud.put(path, path, mtime))


# Ignore


@pytest.mark.parametrize("mode", list(SyncMode))
def test_ignore_rule_keeps_files_out_of_every_direction(
    mode: SyncMode, state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    nas.put("film.mp4", "n")
    icloud.put("clip.MP4", "i")
    nas.put("doc.txt", "d")
    icloud.put("note.txt", "t")

    outcome = _run(_pair(IGNORE_MP4, mode=mode), nas, icloud, state)

    assert {a.path for a in outcome.plan.actions} <= {"doc.txt", "note.txt"}
    assert "clip.MP4" not in nas.files
    assert "film.mp4" not in icloud.files
    hits = {(h.path, h.action) for h in outcome.plan.rule_hits}
    assert hits == {("film.mp4", "ignore"), ("clip.MP4", "ignore")}


def test_ignored_synced_file_is_not_deleted_when_gone_on_one_side(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "a.txt", RECENT)
    _synced(state, nas, icloud, "film.mp4", RECENT)
    icloud.remove("film.mp4")

    outcome = _run(_pair(IGNORE_MP4), nas, icloud, state)

    assert outcome.plan.actions == []
    assert "film.mp4" in nas.files
    assert nas.trash == []


def test_ignore_by_size_uses_either_side(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    big = FileRule("gross", RuleAction.IGNORE, min_size=5)
    icloud.put("big.bin", "123456")
    icloud.put("small.bin", "1")

    _run(_pair(big, mode=SyncMode.DOWNLOAD), nas, icloud, state)

    assert set(nas.files) == {"small.bin"}


# Archive


def test_archive_preview_only_reports(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    preview = FileRule("alt", RuleAction.ARCHIVE, older_than_days=3 * 365)
    _synced(state, nas, icloud, "old.txt", OLD)

    outcome = _run(_pair(preview), nas, icloud, state)

    assert outcome.plan.actions == []
    assert [(h.path, h.pending) for h in outcome.plan.rule_hits] == [("old.txt", "preview")]
    assert "old.txt" in icloud.files
    assert icloud.trash == []


def test_first_run_downloads_but_never_archives(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    icloud.put("old.txt", "x", OLD)

    outcome = _run(_pair(ARCHIVE_OLD, mode=SyncMode.DOWNLOAD), nas, icloud, state)

    assert [a.kind for a in outcome.plan.actions] == [ActionKind.DOWNLOAD]
    assert outcome.plan.rule_hits[0].pending == "first run"
    assert "old.txt" in icloud.files
    assert nas.content("old.txt") == b"x"


@pytest.mark.parametrize("mode", [SyncMode.DOWNLOAD, SyncMode.BIDIRECTIONAL])
def test_archive_trashes_in_icloud_and_keeps_nas_copy(
    mode: SyncMode, state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)
    _synced(state, nas, icloud, "new.txt", RECENT)

    outcome = _run(_pair(ARCHIVE_OLD, mode=mode), nas, icloud, state)

    assert outcome.result is not None and outcome.result.ok
    assert [(a.kind, a.path, a.rule) for a in outcome.plan.actions] == [
        (ActionKind.ARCHIVE, "old.txt", "alt")
    ]
    assert icloud.trashed("old.txt") == [b"old.txt"]
    assert nas.content("old.txt") == b"old.txt"
    assert set(state.records("p")) == {"new.txt"}
    assert state.archived("p") == {"old.txt"}

    again = _run(_pair(ARCHIVE_OLD, mode=mode), nas, icloud, state)

    assert again.plan.actions == []  # not uploaded again, not deleted on the NAS
    assert nas.trash == []


def test_archive_downloads_a_new_file_first(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "keep.txt", RECENT)
    icloud.put("Scans/old.pdf", "pdf", OLD)

    outcome = _run(_pair(ARCHIVE_OLD, mode=SyncMode.DOWNLOAD), nas, icloud, state)

    assert [a.kind for a in outcome.plan.actions] == [ActionKind.ARCHIVE]
    assert nas.content("Scans/old.pdf") == b"pdf"
    assert icloud.trashed("Scans/old.pdf") == [b"pdf"]
    assert state.archived("p") == {"Scans/old.pdf"}


def test_archive_replaces_an_outdated_nas_copy_and_keeps_it_in_the_trash(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)
    icloud.put("old.txt", "edited in iCloud", OLD)

    outcome = _run(_pair(ARCHIVE_OLD, mode=SyncMode.DOWNLOAD), nas, icloud, state)

    assert outcome.result is not None and outcome.result.ok
    assert nas.content("old.txt") == b"edited in iCloud"
    assert nas.trashed("old.txt") == [b"old.txt"]
    assert icloud.trashed("old.txt") == [b"edited in iCloud"]


def test_archive_waits_while_the_nas_copy_changed(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)
    nas.put("old.txt", "edited on the NAS", OLD)

    outcome = _run(_pair(ARCHIVE_OLD), nas, icloud, state)

    assert [a.kind for a in outcome.plan.actions] == [ActionKind.UPLOAD]
    assert outcome.plan.rule_hits[0].pending == "needs another sync"
    assert icloud.content("old.txt") == b"edited on the NAS"


def test_archive_reads_icloud_when_no_checksum_is_known(state: StateDB, nas: FakeProvider) -> None:
    icloud = FakeProvider("icloud", known_hashes=False)
    state.record("p", nas.put("old.txt", "x", OLD), icloud.put("old.txt", "x", OLD))

    outcome = _run(_pair(ARCHIVE_OLD), nas, icloud, state)

    assert outcome.result is not None and outcome.result.ok
    assert icloud.trashed("old.txt") == [b"x"]


class _CorruptingNas(FakeProvider):
    def write(self, path: str, data: BinaryIO, mtime: float) -> FileMeta:
        data.read()
        return self.put(path, b"broken", mtime)


def test_archive_keeps_the_icloud_file_when_the_nas_copy_differs(
    state: StateDB, icloud: FakeProvider
) -> None:
    nas = _CorruptingNas("nas")
    _synced(state, nas, icloud, "keep.txt", RECENT)
    icloud.put("old.txt", "pixels", OLD)

    outcome = _run(_pair(ARCHIVE_OLD, mode=SyncMode.DOWNLOAD), nas, icloud, state)

    assert outcome.result is not None
    assert [f.action.path for f in outcome.result.failed] == ["old.txt"]
    assert "differs from iCloud" in outcome.result.failed[0].error
    assert icloud.content("old.txt") == b"pixels"
    assert icloud.trash == []
    assert state.archived("p") == set()


def test_archive_limit_blocks_until_approved(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    for i in range(3):
        _synced(state, nas, icloud, f"{i}.txt", OLD)
    pair = _pair(ARCHIVE_OLD, archive_limit=2)

    blocked = _run(pair, nas, icloud, state)

    assert blocked.plan.blocked == "3 files would be archived, more than the limit of 2"
    assert blocked.result is not None and blocked.result.blocked
    assert icloud.trash == []

    approved = _run(pair, nas, icloud, state, allow_deletes=True)

    assert approved.result is not None and approved.result.ok
    assert len(icloud.trash) == 3


def test_dry_run_archives_nothing(state: StateDB, nas: FakeProvider, icloud: FakeProvider) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)

    outcome = _run(_pair(ARCHIVE_OLD), nas, icloud, state, dry_run=True)

    assert outcome.plan.archives == 1
    assert icloud.trash == []


def test_archived_file_back_in_icloud_is_synced_again(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)
    _run(_pair(ARCHIVE_OLD), nas, icloud, state)
    icloud.put("old.txt", "old.txt", RECENT)  # restored from the iCloud trash, newer date

    outcome = _run(_pair(ARCHIVE_OLD), nas, icloud, state)

    assert outcome.result is not None and outcome.result.ok
    assert [a.kind for a in outcome.plan.actions] == [ActionKind.RECORD]
    assert state.archived("p") == set()
    assert "old.txt" in state.records("p")


def test_ignore_rule_before_archive_rule_protects_files(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    protect = FileRule("wichtig", RuleAction.IGNORE, patterns=("Steuer/*",))
    _synced(state, nas, icloud, "Steuer/2019.pdf", OLD)
    _synced(state, nas, icloud, "Urlaub/2019.jpg", OLD)

    _run(_pair(protect, ARCHIVE_OLD), nas, icloud, state)

    assert "Steuer/2019.pdf" in icloud.files
    assert "Urlaub/2019.jpg" not in icloud.files


def test_upload_pairs_never_archive(
    state: StateDB, nas: FakeProvider, icloud: FakeProvider
) -> None:
    _synced(state, nas, icloud, "old.txt", OLD)

    outcome = _run(_pair(ARCHIVE_OLD, mode=SyncMode.UPLOAD), nas, icloud, state)

    assert outcome.plan.actions == []
    assert icloud.trash == []
