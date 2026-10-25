from pathlib import Path

from homeward.core.models import FileMeta
from homeward.core.state import StateDB


def test_uses_wal_and_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "acc.sqlite3"
    with StateDB(db_path) as state:
        assert state.journal_mode == "wal"
        state.record("p", FileMeta("a.txt", 3, 1.0, "l1"), FileMeta("a.txt", 3, 2.0, "r1", "abc"))
        state.record("q", FileMeta("a.txt", 3, 1.0, "l2"), FileMeta("a.txt", 3, 2.0, "r2"))
        state.record("q", FileMeta("b.txt", 5, 1.0, "l3"), FileMeta("b.txt", 5, 2.0, "r3"))
        assert state.totals("q") == (2, 8)
        assert state.totals("none") == (0, 0)
    with StateDB(db_path) as state:
        record = state.records("p")["a.txt"]
        assert (record.local_version, record.remote_version, record.sha256) == ("l1", "r1", "abc")
        state.forget("p", "a.txt")
        assert state.records("p") == {}
        assert set(state.records("q")) == {"a.txt", "b.txt"}


def test_archive_replaces_the_record_and_survives_a_reset() -> None:
    state = StateDB(":memory:")
    local = FileMeta("a.txt", 3, 1.0, "l1")
    state.record("p", local, FileMeta("a.txt", 3, 2.0, "r1"))
    state.archive("p", local, "abc", "Alt")
    assert state.records("p") == {}
    assert state.archived("p") == {"a.txt"}
    assert state.archived_totals("p") == (1, 3)
    state.reset("p")
    assert state.archived("p") == {"a.txt"}
    state.reset("p", archived=True)
    assert state.archived("p") == set()


def test_syncing_an_archived_path_again_ends_its_archive_entry() -> None:
    state = StateDB(":memory:")
    local = FileMeta("a.txt", 3, 1.0, "l1")
    state.archive("p", local, "abc", "Alt")
    state.record("p", local, FileMeta("a.txt", 3, 2.0, "r2"))
    assert state.archived("p") == set()
