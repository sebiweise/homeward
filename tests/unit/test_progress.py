import logging
from typing import Any

import pytest

from homeward.core.progress import Progress, ProgressLogHandler


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_counts_rate_and_estimate(clock: Clock) -> None:
    progress = Progress(clock=clock)
    progress.phase("downloading", total=100, total_bytes=1000)
    for _ in range(10):
        clock.now += 1
        progress.advance(nbytes=10, current="2024/01/a.jpg")
    progress.advance(failed=True)
    snap = progress.snapshot()
    assert snap["phase"] == "downloading"
    assert (snap["done"], snap["failed"], snap["total"]) == (11, 1, 100)
    assert snap["bytes"] == 100
    assert snap["byte_rate"] == pytest.approx(10.0)
    assert snap["eta_seconds"] == pytest.approx(90.0)  # 900 bytes left at 10 B/s
    assert snap["current"] == "2024/01/a.jpg"


def test_a_new_phase_starts_at_zero(clock: Clock) -> None:
    progress = Progress(clock=clock)
    progress.phase("listing")
    progress.advance(5)
    progress.total(20)
    assert (progress.snapshot()["done"], progress.snapshot()["total"]) == (5, 20)
    progress.phase("planning", total=3)
    snap = progress.snapshot()
    assert (snap["phase"], snap["done"], snap["total"], snap["eta_seconds"]) == (
        "planning",
        0,
        3,
        None,
    )


def test_sink_is_throttled_but_phase_changes_are_written(clock: Clock) -> None:
    written: list[dict[str, Any]] = []
    progress = Progress(written.append, clock=clock, interval=2.0)
    progress.phase("downloading", total=10)
    progress.advance()
    progress.advance()
    clock.now += 2.5
    progress.advance()
    progress.phase("linking", total=1)
    assert [(s["phase"], s["done"]) for s in written] == [
        ("downloading", 0),
        ("downloading", 3),
        ("linking", 0),
    ]


def test_a_failing_sink_never_breaks_the_run(clock: Clock) -> None:
    def broken(snapshot: dict[str, Any]) -> None:
        raise OSError("disk full")

    progress = Progress(broken, clock=clock)
    progress.phase("downloading", total=1)
    progress.advance()


def test_log_lines_become_the_activity(clock: Clock) -> None:
    progress = Progress(clock=clock)
    logger = logging.getLogger("homeward.test_progress")
    handler = ProgressLogHandler(progress)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("iCloud is still indexing", extra={"until": 1060.0})
        snap = progress.snapshot()
        assert (snap["activity"], snap["waiting_until"]) == ("iCloud is still indexing", 1060.0)
        progress.advance()  # work goes on: the wait is over
        assert progress.snapshot()["waiting_until"] is None
    finally:
        logger.removeHandler(handler)
