import io
from datetime import datetime
from pathlib import Path

import pytest

from homeward.core.limits import Throttle, ThrottledReader, TimeWindow, throttle_for, throttled


def _at(hour: int, minute: int = 0, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def test_window_parse_and_text() -> None:
    assert str(TimeWindow.parse(" 22:00 - 6:30 ")) == "22:00-06:30"
    whole_day = TimeWindow.parse("00:00-24:00")
    assert whole_day.contains(_at(0)) and whole_day.contains(_at(23, 59))
    assert TimeWindow.parse(str(whole_day)) == whole_day  # saved, then read again
    for bad in ("22-06", "25:00-06:00", "10:00-10:00", "10:60-11:00", ""):
        with pytest.raises(ValueError):
            TimeWindow.parse(bad)


def test_window_during_the_day() -> None:
    window = TimeWindow.parse("09:00-17:00")
    assert window.contains(_at(9)) and window.contains(_at(16, 59))
    assert not window.contains(_at(17)) and not window.contains(_at(8, 59))
    assert window.next_open(_at(12)) == _at(12)
    assert window.next_open(_at(7)) == _at(9)
    assert window.next_open(_at(18)) == _at(9, day=16)


def test_window_over_midnight() -> None:
    window = TimeWindow.parse("22:00-06:00")
    assert window.contains(_at(23)) and window.contains(_at(2)) and not window.contains(_at(6))
    assert window.next_open(_at(12)) == _at(22)
    assert window.next_open(_at(3)) == _at(3)


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_throttle_keeps_the_rate() -> None:
    fake = FakeTime()
    throttle = Throttle(1000, clock=fake.clock, sleep=fake.sleep)
    for _ in range(10):
        throttle.consume(500)
    assert fake.now == pytest.approx(105.0)  # 5000 bytes at 1000 B/s

    fake.now += 60  # a long pause makes up for at most one second
    throttle.consume(2000)
    assert fake.slept[-1] == pytest.approx(1.0)


def test_throttled_reader_reads_and_reports_its_size() -> None:
    fake = FakeTime()
    stream = ThrottledReader(io.BytesIO(b"x" * 3000), Throttle(1000, fake.clock, fake.sleep))
    assert stream.seek(0, io.SEEK_END) == 3000 and stream.seek(0) == 0 and stream.tell() == 0
    with stream:
        assert len(stream.read(2000)) == 2000
        assert stream.read() == b"x" * 1000
    assert fake.now == pytest.approx(103.0)


def test_no_limit_means_no_wrapper() -> None:
    raw = io.BytesIO(b"")
    assert throttled(raw, None) is raw
    assert throttle_for(None) is None and throttle_for(0) is None
    throttle = throttle_for(500)
    assert throttle is not None and throttle.rate == 500_000
    with pytest.raises(ValueError):
        Throttle(0)


def test_a_sync_run_goes_through_the_throttle(tmp_path: Path) -> None:
    from homeward.core.executor import Executor
    from homeward.core.models import SyncMode
    from homeward.core.planner import build_plan
    from homeward.core.state import StateDB
    from homeward.providers.nas import NasProvider

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.bin").write_bytes(b"x" * 4000)
    local, remote = NasProvider(tmp_path / "a"), NasProvider(tmp_path / "b")
    fake = FakeTime()
    with StateDB(tmp_path / "state.sqlite3") as state:
        plan = build_plan("p", SyncMode.UPLOAD, local.list_files(), remote.list_files(), {})
        throttle = Throttle(1000, fake.clock, fake.sleep)
        result = Executor(local, remote, state, "p", throttle=throttle).run(plan)
    assert result.ok and (tmp_path / "b" / "x.bin").stat().st_size == 4000
    assert fake.now == pytest.approx(104.0)
