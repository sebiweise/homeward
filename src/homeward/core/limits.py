"""Per-job limits: the hours a job may run in, and the transfer rate it may use."""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import BinaryIO, Self

_WINDOW = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-4]):([0-5]\d)\s*$")


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Daily hours, in the NAS's local time, e.g. 22:00-06:00 (over midnight)."""

    start: int  # minutes after midnight
    end: int

    @classmethod
    def parse(cls, text: str) -> Self:
        match = _WINDOW.match(text)
        if not match:
            raise ValueError(f"time window {text!r}: expected HH:MM-HH:MM, e.g. 22:00-06:00")
        h1, m1, h2, m2 = (int(group) for group in match.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end > 24 * 60 or start == end:
            raise ValueError(f"time window {text!r}: start and end must differ")
        return cls(start, end % (24 * 60))

    def __str__(self) -> str:
        return f"{_clock_time(self.start)}-{_clock_time(self.end)}"

    def contains(self, at: float) -> bool:
        minute = _minute(at)
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end  # over midnight

    def next_open(self, at: float) -> float:
        """``at`` if the window is open then, else the time it opens next."""
        if self.contains(at):
            return at
        moment = datetime.fromtimestamp(at)
        opens = moment.replace(hour=self.start // 60, minute=self.start % 60, second=0,
                               microsecond=0)  # fmt: skip
        if opens <= moment:
            opens += timedelta(days=1)
        return opens.timestamp()


def _clock_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minute(at: float) -> int:
    moment = datetime.fromtimestamp(at)
    return moment.hour * 60 + moment.minute


class Throttle:
    """Keeps the bytes passed through ``consume`` at ``rate`` bytes per second, over
    all threads that share it (the parallel photo downloads of one run)."""

    BURST_SECONDS = 1.0  # unused time a pause may make up for

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = clock()

    def consume(self, size: int) -> None:
        with self._lock:
            now = self._clock()
            self._next = max(self._next, now - self.BURST_SECONDS) + size / self.rate
            delay = self._next - now
        if delay > 0:
            self._sleep(delay)


class ThrottledReader:
    """A binary stream whose reads wait for ``throttle``. Size queries (``tell``,
    ``seek``) go to the wrapped stream, so an upload can still announce its length."""

    def __init__(self, stream: BinaryIO, throttle: Throttle) -> None:
        self._stream = stream
        self._throttle = throttle

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self._throttle.consume(len(data))
        return data

    def tell(self) -> int:
        return self._stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def throttled(stream: BinaryIO, throttle: Throttle | None) -> BinaryIO:
    """``stream``, limited to the throttle's rate if there is one."""
    if throttle is None:
        return stream
    return ThrottledReader(stream, throttle)  # type: ignore[return-value]


def throttle_for(max_rate_kb: int | None) -> Throttle | None:
    """A throttle for a job's ``max_rate_kb`` (KB/s, as in DSM), None for no limit."""
    return Throttle(max_rate_kb * 1000) if max_rate_kb else None
