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
