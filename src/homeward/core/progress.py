"""How far a running sync or photos backup is, for the progress shown in the DSM UI.

A run reports its phases (e.g. reading iCloud, downloading) and every finished
file to a ``Progress``. It hands a snapshot to its ``sink`` at most every
``interval`` seconds and whenever the phase changes; the CLI's sink writes it to a
file the UI API reads. Without a sink nothing is written. All methods are
thread-safe and never raise into the run.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Sink = Callable[[dict[str, Any]], None]

# Phases the UI has texts for (``[progress]`` section of the strings)
PHASES = (
    "connecting",  # signing in to iCloud
    "checking",  # asking iCloud whether anything changed since the last run
    "listing",  # reading the photo library or the folders on both sides
    "albums",  # reading the albums
    "planning",  # comparing with the last run
    "downloading",  # photo downloads
    "transferring",  # sync pair: uploads, downloads, moves and deletes
    "linking",  # album links and the remaining bookkeeping
)
WRITE_INTERVAL = 2.0
RATE_WINDOW = 60.0  # seconds of recent transfers the speed is measured over


class Progress:
    def __init__(
        self,
        sink: Sink | None = None,
        *,
        clock: Callable[[], float] = time.time,
        interval: float = WRITE_INTERVAL,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._started = clock()
        self._written = 0.0
        self._phase: str | None = None
        self._phase_started = self._started
        self._done = 0
        self._failed = 0
        self._total: int | None = None
        self._bytes = 0
        self._total_bytes: int | None = None
        self._current: str | None = None
        self._activity: str | None = None
        self._activity_at: float | None = None
        self._waiting_until: float | None = None
        self._samples: list[tuple[float, int, int]] = []  # (time, done, bytes)

    def phase(self, name: str, total: int | None = None, total_bytes: int | None = None) -> None:
        """Start a new phase; its counters start at zero."""
        with self._lock:
            now = self._clock()
            self._phase, self._phase_started = name, now
            self._done = self._failed = self._bytes = 0
            self._total, self._total_bytes = total, total_bytes
            self._current = None
            self._samples = [(now, 0, 0)]
        self._write(force=True)

    def total(self, total: int | None) -> None:
        """Set or correct the number of steps of the current phase (e.g. once known)."""
        with self._lock:
            self._total = total
        self._write()

    def advance(
        self, steps: int = 1, *, nbytes: int = 0, failed: bool = False, current: str | None = None
    ) -> None:
        """``steps`` more of the current phase are done (failed ones count as done)."""
        with self._lock:
            now = self._clock()
            self._done += steps
            self._failed += steps if failed else 0
            self._bytes += nbytes
            if current is not None:
                self._current = current
            self._waiting_until = None
            self._samples.append((now, self._done, self._bytes))
            while len(self._samples) > 2 and self._samples[1][0] < now - RATE_WINDOW:
                self._samples.pop(0)
        self._write()

    def activity(self, text: str, until: float | None = None) -> None:
        """What the run is doing right now, e.g. a log line; ``until`` while it waits."""
        with self._lock:
            self._activity, self._activity_at = text, self._clock()
            self._waiting_until = until
        self._write()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            first_time, first_done, first_bytes = self._samples[0] if self._samples else (now, 0, 0)
            span = now - first_time
            rate = (self._done - first_done) / span if span >= 1 else None
            byte_rate = (self._bytes - first_bytes) / span if span >= 1 else None
            eta = None
            if self._total is not None and rate:
                eta = max(self._total - self._done, 0) / rate
            if self._total_bytes and byte_rate:
                eta = max(self._total_bytes - self._bytes, 0) / byte_rate
            return {
                "started_at": self._started,
                "updated_at": now,
                "phase": self._phase,
                "phase_started_at": self._phase_started,
                "done": self._done,
                "failed": self._failed,
                "total": self._total,
                "bytes": self._bytes,
                "total_bytes": self._total_bytes,
                "rate": rate,  # steps per second
                "byte_rate": byte_rate,
                "eta_seconds": eta,
                "current": self._current,
                "activity": self._activity,
                "activity_at": self._activity_at,
                "waiting_until": self._waiting_until,
            }

    def flush(self) -> None:
        self._write(force=True)

    def _write(self, *, force: bool = False) -> None:
        if self._sink is None:
            return
        now = self._clock()
        with self._lock:
            if not force and now - self._written < self._interval:
                return
            self._written = now
        try:
            self._sink(self.snapshot())
        except Exception as exc:  # progress must never break a run
            log.debug("could not write progress: %s", exc)


class ProgressLogHandler(logging.Handler):
    """Shows the run's log lines (INFO and up) as its current activity."""

    def __init__(self, progress: Progress) -> None:
        super().__init__(logging.INFO)
        self.progress = progress

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        until = getattr(record, "until", None)
        self.progress.activity(record.getMessage(), float(until) if until is not None else None)
