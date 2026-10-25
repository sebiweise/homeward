"""Per-account SQLite state database (WAL mode)."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from homeward.core.models import FileMeta, FileRecord

# 2: archived files. An older Homeward must not open it: it would upload them again.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    pair TEXT NOT NULL,
    path TEXT NOT NULL,
    local_version TEXT NOT NULL,
    remote_version TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT,
    synced_at REAL NOT NULL,
    PRIMARY KEY (pair, path)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    dry_run INTEGER NOT NULL,
    actions INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    blocked TEXT
);
CREATE TABLE IF NOT EXISTS archived (
    pair TEXT NOT NULL,
    path TEXT NOT NULL,
    local_version TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    rule TEXT NOT NULL,
    archived_at REAL NOT NULL,
    PRIMARY KEY (pair, path)
);
"""


@dataclass(frozen=True, slots=True)
class RunRecord:
    pair: str
    started_at: float
    finished_at: float
    dry_run: bool
    actions: int
    errors: int
    blocked: str | None


class StateDB:
    """Last common state of every synced file, one database file per account."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"state database {path} is newer than this Homeward version")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def records(self, pair: str) -> dict[str, FileRecord]:
        rows = self._conn.execute(
            "SELECT path, local_version, remote_version, size, sha256, synced_at "
            "FROM files WHERE pair = ?",
            (pair,),
        )
        return {row[0]: FileRecord(*row) for row in rows}

    def record(self, pair: str, local: FileMeta, remote: FileMeta) -> FileRecord:
        """Remember that both sides hold the same content for ``local.path``."""
        rec = FileRecord(
            path=local.path,
            local_version=local.version,
            remote_version=remote.version,
            size=local.size,
            sha256=local.sha256 or remote.sha256,
            synced_at=time.time(),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pair, rec.path, rec.local_version, rec.remote_version, rec.size, rec.sha256,
             rec.synced_at),
        )  # fmt: skip
        # Synced again (it came back to iCloud): no longer an archived NAS-only copy.
        self._conn.execute("DELETE FROM archived WHERE pair = ? AND path = ?", (pair, rec.path))
        return rec

    def archive(self, pair: str, local: FileMeta, sha256: str, rule: str) -> None:
        """Remember that ``local`` was removed from iCloud and lives on only on the NAS."""
        self._conn.execute("DELETE FROM files WHERE pair = ? AND path = ?", (pair, local.path))
        self._conn.execute(
            "INSERT OR REPLACE INTO archived VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pair, local.path, local.version, local.size, sha256, rule, time.time()),
        )

    def archived(self, pair: str) -> set[str]:
        """Paths of the pair's archived files."""
        rows = self._conn.execute("SELECT path FROM archived WHERE pair = ?", (pair,))
        return {row[0] for row in rows}

    def archived_totals(self, pair: str) -> tuple[int, int]:
        """Number and total size of the pair's archived files."""
        count, size = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM archived WHERE pair = ?", (pair,)
        ).fetchone()
        return int(count), int(size)

    def forget(self, pair: str, path: str) -> None:
        self._conn.execute("DELETE FROM files WHERE pair = ? AND path = ?", (pair, path))

    def reset(self, pair: str, *, archived: bool = False) -> int:
        """Forget every file of a pair, so its next run is a first run again.

        Archived files stay remembered unless ``archived`` is set (the pair is
        removed): otherwise a bidirectional pair would upload them again.
        """
        cursor = self._conn.execute("DELETE FROM files WHERE pair = ?", (pair,))
        if archived:
            self._conn.execute("DELETE FROM archived WHERE pair = ?", (pair,))
        return cursor.rowcount

    def totals(self, pair: str) -> tuple[int, int]:
        """Number and total size of the files the pair keeps in sync."""
        count, size = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE pair = ?", (pair,)
        ).fetchone()
        return int(count), int(size)

    def runs(self, pair: str | None = None, limit: int = 50) -> list[RunRecord]:
        """The most recent runs, newest first."""
        query = "SELECT pair, started_at, finished_at, dry_run, actions, errors, blocked FROM runs"
        args: tuple[object, ...] = ()
        if pair is not None:
            query += " WHERE pair = ?"
            args = (pair,)
        rows = self._conn.execute(query + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [
            RunRecord(pair, started, finished, bool(dry), actions, errors, blocked)
            for pair, started, finished, dry, actions, errors, blocked in rows
        ]

    def log_run(
        self,
        pair: str,
        started_at: float,
        *,
        dry_run: bool,
        actions: int,
        errors: int,
        blocked: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO runs (pair, started_at, finished_at, dry_run, actions, errors, blocked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pair, started_at, time.time(), int(dry_run), actions, errors, blocked),
        )
