"""Per-account SQLite state of the photos backup (WAL), separate from the Drive state."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    job TEXT NOT NULL,
    key TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    checksum TEXT,
    synced_at REAL NOT NULL,
    gone_at REAL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS album_links (
    job TEXT NOT NULL,
    path TEXT NOT NULL,
    key TEXT NOT NULL,
    album TEXT NOT NULL,
    PRIMARY KEY (job, path)
);
CREATE TABLE IF NOT EXISTS cursors (
    job TEXT PRIMARY KEY,
    cursor TEXT
);
CREATE TABLE IF NOT EXISTS integrity_checks (
    job TEXT PRIMARY KEY,
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS full_listings (
    job TEXT PRIMARY KEY,
    finished_at REAL NOT NULL,
    selection TEXT
);
"""


@dataclass(frozen=True, slots=True)
class PhotoRecord:
    """A photo file that is on the NAS. ``gone_at`` is set once iCloud no longer has it."""

    key: str
    path: str
    size: int | None
    checksum: str | None
    synced_at: float
    gone_at: float | None


class PhotoStateDB:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"photo state database {path} is newer than this Homeward version")
        self._conn.executescript(_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(full_listings)")}
        if "selection" not in columns:  # test builds 0.2.0-3 created it without
            self._conn.execute("ALTER TABLE full_listings ADD COLUMN selection TEXT")
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

    def records(self, job: str) -> dict[str, PhotoRecord]:
        rows = self._conn.execute(
            "SELECT key, path, size, checksum, synced_at, gone_at FROM photos WHERE job = ?",
            (job,),
        )
        return {row[0]: PhotoRecord(*row) for row in rows}

    def record(self, job: str, key: str, path: str, size: int, checksum: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO photos VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (job, key, path, size, checksum, time.time()),
        )

    def forget(self, job: str, key: str) -> None:
        """The photo was removed on the NAS too: if it comes back, it is new."""
        self._conn.execute("DELETE FROM photos WHERE job = ? AND key = ?", (job, key))

    def mark_gone(self, job: str, key: str) -> None:
        self._conn.execute(
            "UPDATE photos SET gone_at = ? WHERE job = ? AND key = ?", (time.time(), job, key)
        )

    def links(self, job: str) -> dict[str, tuple[str, str]]:
        """Album links of a job: link path -> (photo key, album name)."""
        rows = self._conn.execute("SELECT path, key, album FROM album_links WHERE job = ?", (job,))
        return {row[0]: (row[1], row[2]) for row in rows}

    def add_link(self, job: str, path: str, key: str, album: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO album_links VALUES (?, ?, ?, ?)", (job, path, key, album)
        )

    def remove_link(self, job: str, path: str) -> None:
        self._conn.execute("DELETE FROM album_links WHERE job = ? AND path = ?", (job, path))

    def cursor(self, job: str) -> str | None:
        row = self._conn.execute("SELECT cursor FROM cursors WHERE job = ?", (job,)).fetchone()
        return None if row is None else row[0]

    def set_cursor(self, job: str, cursor: str | None) -> None:
        self._conn.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (job, cursor))

    def full_listing_at(self, job: str) -> float | None:
        """When a run that read the whole library last finished without errors."""
        row = self._conn.execute(
            "SELECT finished_at FROM full_listings WHERE job = ?", (job,)
        ).fetchone()
        return None if row is None else float(row[0])

    def full_listing_selection(self, job: str) -> str | None:
        """What that run selected (libraries and file types), see ``set_full_listing``."""
        row = self._conn.execute(
            "SELECT selection FROM full_listings WHERE job = ?", (job,)
        ).fetchone()
        return None if row is None else row[0]

    def set_full_listing(self, job: str, at: float, selection: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO full_listings VALUES (?, ?, ?)", (job, at, selection)
        )

    def integrity_checked_at(self, job: str) -> float | None:
        row = self._conn.execute(
            "SELECT checked_at FROM integrity_checks WHERE job = ?", (job,)
        ).fetchone()
        return None if row is None else float(row[0])

    def set_integrity_checked(self, job: str, at: float) -> None:
        self._conn.execute("INSERT OR REPLACE INTO integrity_checks VALUES (?, ?)", (job, at))

    def clear_full_listing(self, job: str) -> None:
        """Make the next run read the whole library (its selection still counts)."""
        self._conn.execute("UPDATE full_listings SET finished_at = 0 WHERE job = ?", (job,))


def gone_counts(path: Path) -> dict[str, int]:
    """Per job, the photos still on the NAS that iCloud no longer has.

    Opens the database read-only (for the UI, also while a run writes to it); a
    missing or unreadable database counts as none.
    """
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT job, COUNT(*) FROM photos WHERE gone_at IS NOT NULL GROUP BY job"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(job): int(count) for job, count in rows}


def totals(path: Path) -> dict[str, tuple[int, int]]:
    """Per job, the number and total size of the photos on the NAS (read-only, as
    ``gone_counts``)."""
    return _read_only(path, "SELECT job, COUNT(*), COALESCE(SUM(size), 0) FROM photos GROUP BY job")


def _read_only(path: Path, query: str) -> dict[str, tuple[int, int]]:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(name): (int(count), int(size)) for name, count, size in rows}
