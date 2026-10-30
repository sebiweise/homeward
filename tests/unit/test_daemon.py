import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from homeward.core.config import ConfigError
from homeward.daemon.scheduler import (
    MAX_SLEEP_SECONDS,
    WAITING_RETRY_MINUTES,
    Daemon,
    Job,
    Scheduler,
    Settings,
    SubprocessRunner,
    load_plan,
    main,
    parse_settings,
)
from homeward.runtime import take_requests


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[str] = []

    def __call__(self, job: Job) -> int:
        self.calls.append(job.name)
        return self.code


def _config(tmp_path: Path, daemon: str = "", pairs: tuple[str, ...] = ("docs",)) -> Path:
    (tmp_path / "nas").mkdir(exist_ok=True)
    (tmp_path / "cloud").mkdir(exist_ok=True)
    text = f'state_dir = "state"\n{daemon}\n'
    for name in pairs:
        text += f"""
[[pair]]
name = "{name}"
local = "{tmp_path / "nas"}"
remote = "{tmp_path / "cloud"}"
remote_provider = "folder"
mode = "upload"
"""
    config = tmp_path / "config.toml"
    config.write_text(text)
    return config


def _scheduler(tmp_path: Path, runner: Recorder, clock: Clock) -> Scheduler:
    return Scheduler(tmp_path / "status.json", runner, clock)


def test_parse_settings() -> None:
    assert parse_settings({}) == Settings(60)
    assert parse_settings({"interval_minutes": 15}).interval_seconds == 900
    assert parse_settings({"interval_minutes": 10080}).interval_minutes == 10080
    bad_values = (1, 10081, "5", True, 7.5)
    for bad in ({"interval_minutes": value} for value in bad_values):
        with pytest.raises(ConfigError):
            parse_settings(bad)
    with pytest.raises(ConfigError):
        parse_settings([])


def test_load_plan_makes_one_job_per_pair(tmp_path: Path) -> None:
    config = _config(tmp_path, "[daemon]\ninterval_minutes = 30", ("docs", "pics"))
    plan = load_plan(config)
    assert plan.settings.interval_minutes == 30
    assert [job.name for job in plan.jobs] == ["pair:docs", "pair:pics"]
    assert plan.jobs[0].argv == ("sync", "--config", str(config), "--pair", "docs")


def test_jobs_run_once_per_interval(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder()
    scheduler = _scheduler(tmp_path, runner, clock)
    scheduler.update(load_plan(_config(tmp_path, "[daemon]\ninterval_minutes = 10")))

    scheduler.run_due()
    assert runner.calls == ["pair:docs"]
    clock.now += 9 * 60
    assert scheduler.run_due() == []
    assert scheduler.seconds_until_next() == 10  # capped, to notice config changes
    clock.now += 60
    scheduler.run_due()
    assert runner.calls == ["pair:docs", "pair:docs"]


def test_a_job_waiting_for_icloud_is_tried_again_soon(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder(code=4)  # EXIT_WAITING
    scheduler = _scheduler(tmp_path, runner, clock)
    scheduler.update(load_plan(_config(tmp_path, "[daemon]\ninterval_minutes = 360")))

    scheduler.run_due()
    assert scheduler.status["pair:docs"].result == "waiting"
    assert (
        json.loads((tmp_path / "status.json").read_text())["jobs"]["pair:docs"]["next_due"]
        == clock.now + WAITING_RETRY_MINUTES * 60
    )
    clock.now += (WAITING_RETRY_MINUTES - 1) * 60
    assert scheduler.run_due() == []
    clock.now += 60
    runner.code = 0
    assert len(scheduler.run_due()) == 1
    clock.now += WAITING_RETRY_MINUTES * 60
    assert scheduler.run_due() == []  # back to the normal interval


def test_a_pair_can_have_its_own_interval(tmp_path: Path) -> None:
    config = _config(tmp_path, "[daemon]\ninterval_minutes = 60", ("docs", "pics"))
    text = config.read_text().replace('name = "pics"\n', 'name = "pics"\ninterval_minutes = 15\n')
    config.write_text(text)
    clock, runner = Clock(), Recorder()
    scheduler = _scheduler(tmp_path, runner, clock)
    plan = load_plan(config)
    assert [job.interval_minutes for job in plan.jobs] == [None, 15]
    scheduler.update(plan)

    scheduler.run_due()
    clock.now += 15 * 60
    scheduler.run_due()
    assert runner.calls == ["pair:docs", "pair:pics", "pair:pics"]

    jobs = json.loads((tmp_path / "status.json").read_text())["jobs"]
    assert jobs["pair:docs"]["interval_minutes"] == 60
    assert jobs["pair:pics"]["interval_minutes"] == 15
    assert jobs["pair:pics"]["next_due"] == clock.now + 15 * 60


def test_a_changed_interval_applies_to_the_next_run(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder()
    scheduler = _scheduler(tmp_path, runner, clock)
    scheduler.update(load_plan(_config(tmp_path, "[daemon]\ninterval_minutes = 60")))
    scheduler.run_due()
    clock.now += 20 * 60
    assert scheduler.due() == []
    scheduler.update(load_plan(_config(tmp_path, "[daemon]\ninterval_minutes = 15")))
    assert [job.name for job in scheduler.due()] == ["pair:docs"]


def test_status_survives_a_restart(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder(code=3)
    plan = load_plan(_config(tmp_path))
    first = _scheduler(tmp_path, runner, clock)
    first.update(plan)
    first.run_due()

    status = json.loads((tmp_path / "status.json").read_text())
    job = status["jobs"]["pair:docs"]
    assert job["result"] == "blocked"
    assert job["exit_code"] == 3
    assert job["next_due"] == clock.now + 3600

    second = _scheduler(tmp_path, runner, clock)
    second.update(plan)
    assert second.due() == []


def test_unreadable_status_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "status.json").write_text("{not json")
    scheduler = _scheduler(tmp_path, Recorder(), Clock())
    assert scheduler.status == {}


def test_run_all_now(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder()
    scheduler = _scheduler(tmp_path, runner, clock)
    scheduler.update(load_plan(_config(tmp_path, pairs=("a", "b"))))
    scheduler.run_due()
    assert scheduler.due() == []
    scheduler.run_all_now()
    scheduler.run_due()
    assert runner.calls == ["pair:a", "pair:b", "pair:a", "pair:b"]


def test_runner_errors_are_recorded(tmp_path: Path) -> None:
    def broken(job: Job) -> int:
        raise OSError("no such file")

    scheduler = Scheduler(tmp_path / "status.json", broken, Clock())
    status = scheduler.run(Job("pair:x", ()))
    assert status.result == "failed"
    assert status.exit_code == 127


def test_interrupted_runs_are_due_again(tmp_path: Path) -> None:
    clock = Clock()
    scheduler = _scheduler(tmp_path, Recorder(code=-15), clock)
    scheduler.update(load_plan(_config(tmp_path)))
    scheduler.run_due()
    assert scheduler.status["pair:docs"].result == "killed"
    assert [job.name for job in scheduler.due()] == ["pair:docs"]

    # A daemon that died mid-run left no finished_at behind.
    scheduler.status["pair:docs"].result = None
    scheduler.status["pair:docs"].finished_at = None
    assert [job.name for job in scheduler.due()] == ["pair:docs"]


def test_removed_pairs_leave_the_status_file(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder()
    scheduler = _scheduler(tmp_path, runner, clock)
    scheduler.update(load_plan(_config(tmp_path, pairs=("a", "b"))))
    scheduler.run_due()
    scheduler.update(load_plan(_config(tmp_path, pairs=("a",))))
    scheduler.save()
    assert list(json.loads((tmp_path / "status.json").read_text())["jobs"]) == ["pair:a"]


def test_daemon_waits_for_a_valid_config(tmp_path: Path) -> None:
    runner = SubprocessRunner()
    recorder = Recorder()
    config = tmp_path / "config.toml"
    daemon = Daemon(config, _scheduler(tmp_path, recorder, Clock()), runner)

    _cycle(daemon)  # missing file
    assert not daemon.config_ok
    assert recorder.calls == []
    _config(tmp_path)
    _cycle(daemon)
    assert daemon.config_ok
    assert recorder.calls == ["pair:docs"]


def test_once_runs_a_real_sync(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "nas" / "a.txt").write_text("a")
    assert main(["--config", str(config), "--once"]) == 0
    assert (tmp_path / "cloud" / "a.txt").read_text() == "a"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["jobs"]["pair:docs"]["result"] == "ok"


def test_once_reports_a_broken_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[daemon]\ninterval_minutes = 0\n")
    assert main(["--config", str(config), "--once"]) == 2


def _request(tmp_path: Path, name: str, data: str) -> None:
    (tmp_path / "requests").mkdir(exist_ok=True)
    (tmp_path / "requests" / f"{name}.json").write_text(data)


def test_take_requests(tmp_path: Path) -> None:
    assert take_requests(tmp_path / "requests") == []
    _request(tmp_path, "a", '{"pair": "a", "dry_run": true}')
    _request(tmp_path, "b", "{broken")
    requests = take_requests(tmp_path / "requests")
    assert [(r.pair, r.dry_run, r.allow_deletes) for r in requests] == [("a", True, False)]
    assert list((tmp_path / "requests").iterdir()) == []


def _cycle(daemon: Daemon) -> None:
    """One daemon cycle, waiting for the runs it started (they run on threads)."""
    daemon.cycle()
    daemon.scheduler.wait()


class ArgvRecorder:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []

    def __call__(self, job: Job) -> int:
        self.argv.append(job.argv)
        return 0


def test_daemon_runs_requests(tmp_path: Path) -> None:
    clock, runner = Clock(), ArgvRecorder()
    config = _config(tmp_path, pairs=("a",))
    scheduler = Scheduler(tmp_path / "status.json", runner, clock)
    daemon = Daemon(config, scheduler, SubprocessRunner())
    _cycle(daemon)  # scheduled run
    assert len(runner.argv) == 1
    scheduled_at = scheduler.status["pair:a"].started_at

    _request(tmp_path, "a", '{"pair": "a", "dry_run": true}')
    _request(tmp_path, "zzz", '{"pair": "missing"}')
    _cycle(daemon)
    assert runner.argv[1][-1] == "--dry-run"
    assert len(runner.argv) == 2
    # A dry run does not count as the scheduled run.
    assert scheduler.status["pair:a"].started_at == scheduled_at

    clock.now += 60
    _request(tmp_path, "a", '{"pair": "a", "allow_deletes": true}')
    _cycle(daemon)
    assert runner.argv[2][-1] == "--allow-deletes"
    assert scheduler.status["pair:a"].started_at == clock.now
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["running"] is None


def test_photos_jobs_are_scheduled_and_requestable(tmp_path: Path) -> None:
    config = _config(tmp_path, pairs=("a",))
    with config.open("a") as fh:
        fh.write(
            f"""
[[photos]]
name = "me"
account = "me"
apple_id = "me@example.com"
target = "{tmp_path / "photos"}"
"""
        )
    plan = load_plan(config)
    assert [job.name for job in plan.jobs] == ["pair:a", "photos:me"]
    assert plan.jobs[1].argv == ("photos", "--config", str(config), "--job", "me")

    runner = ArgvRecorder()
    daemon = Daemon(
        config, Scheduler(tmp_path / "status.json", runner, Clock()), SubprocessRunner()
    )
    _cycle(daemon)
    _request(tmp_path, "p", '{"pair": "photos:me", "dry_run": true, "allow_deletes": true}')
    _cycle(daemon)
    assert runner.argv[-1] == (
        "photos", "--config", str(config), "--job", "me", "--dry-run", "--allow-deletes"
    )  # fmt: skip
    _request(tmp_path, "p", '{"pair": "photos:me", "remove_gone": true}')
    _cycle(daemon)
    assert runner.argv[-1] == (
        "photos", "--config", str(config), "--job", "me", "--remove-gone"
    )  # fmt: skip


def test_runs_write_events_and_notify_once(tmp_path: Path) -> None:
    from homeward.runtime import Paths, read_events

    sent: list[tuple[str, str]] = []
    runner = Recorder(code=1)
    scheduler = Scheduler(
        tmp_path / "status.json", runner, Clock(), notifier=lambda e, *a: sent.append((e, *a))
    )
    job = Job("pair:docs", ("sync",))
    scheduler.run(job)
    scheduler.run(job)
    runner.code = 3
    scheduler.run(job)
    runner.code = 0
    scheduler.run(job)

    events = read_events(Paths(var_dir=tmp_path, config=tmp_path / "config.toml"))
    codes = [(e.source, e.code) for e in reversed(events) if e.code != "sync_started"]
    assert codes == [("pair:docs", c) for c in ("sync_failed", "sync_failed", "delete_brake",
                                                "sync_finished")]  # fmt: skip
    assert sent == [("sync_failed", "docs"), ("delete_brake", "docs")]


def test_a_needed_sign_in_notifies_once_per_apple_id(tmp_path: Path) -> None:
    sent: list[tuple[str, ...]] = []
    runner = Recorder(code=6)
    scheduler = Scheduler(
        tmp_path / "status.json", runner, Clock(), notifier=lambda e, *a: sent.append((e, *a))
    )
    photos, drive = (
        Job("photos:me", ("photos",), account="me"),
        Job("pair:d", ("sync",), account="me"),
    )
    other = Job("photos:anna", ("photos",), account="anna")
    scheduler.jobs = [photos, drive, other]
    for job in (photos, drive, photos, other):
        assert scheduler.run(job).result == "login_required"

    assert sent == [("login_needed", "me"), ("login_needed", "anna")]
    runner.code = 0
    scheduler.run(photos)
    scheduler.run(drive)
    runner.code = 6
    scheduler.run(photos)  # signed out again later
    assert sent[-1] == ("login_needed", "me")


def test_config_errors_become_events(tmp_path: Path) -> None:
    from homeward.runtime import Paths, read_events

    config = tmp_path / "config.toml"
    config.write_text("[[pair]]\nname = 1\n")
    daemon = Daemon(config, _scheduler(tmp_path, Recorder(), Clock()), SubprocessRunner())
    assert not daemon.reload()
    assert not daemon.reload()  # the same error is recorded once
    events = read_events(Paths(var_dir=tmp_path, config=config))
    assert [(e.source, e.code) for e in events] == [("service", "config_error")]


def test_a_stopped_run_is_a_warning_not_an_error(tmp_path: Path) -> None:
    from homeward.runtime import Paths, read_events

    sent: list[str] = []
    scheduler = Scheduler(
        tmp_path / "status.json", Recorder(code=-15), Clock(), notifier=lambda e, *a: sent.append(e)
    )
    scheduler.run(Job("photos:me", ("photos",)))
    (event, _started) = read_events(Paths(var_dir=tmp_path, config=tmp_path / "config.toml"))
    assert (event.level, event.code) == ("warning", "sync_stopped")
    assert sent == []


class StoppableRunner(SubprocessRunner):
    """Stands in for the sync process; ``terminate`` makes it end like one on SIGTERM."""

    def __init__(self, during: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self.during = during
        self.terminated = 0
        self.calls: list[str] = []

    def __call__(self, job: Job) -> int:
        self.calls.append(job.name)
        self.during()
        return 5 if self.terminated else 0

    def terminate(self, name: str | None = None, timeout: float = 0.0) -> None:
        self.terminated += 1


def test_cancelling_the_running_sync(tmp_path: Path) -> None:
    from homeward.runtime import CancelRequest, Paths, read_events, request_cancel

    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    config = _config(tmp_path, pairs=("a",))
    sent: list[str] = []
    clock = Clock()

    def cancel_while_running() -> None:
        request_cancel(paths, CancelRequest("other"))  # not running: ignored
        request_cancel(paths, CancelRequest("a"))
        daemon.check_cancels()
        request_cancel(paths, CancelRequest("a"))  # a second click changes nothing
        daemon.check_cancels()

    runner = StoppableRunner(cancel_while_running)
    scheduler = Scheduler(
        tmp_path / "status.json", runner, clock, notifier=lambda e, *a: sent.append(e)
    )
    daemon = Daemon(config, scheduler, runner)
    _cycle(daemon)

    assert runner.terminated == 1
    status = scheduler.status["pair:a"]
    assert (status.result, status.exit_code) == ("cancelled", 5)
    assert scheduler.running == set() and scheduler.cancelled == set()
    assert not list((tmp_path / "cancel").glob("*.json"))
    (event, _started) = read_events(paths)
    assert (event.level, event.code, event.source) == ("warning", "sync_cancelled", "pair:a")
    assert sent == []  # cancelling is not an error worth a notification
    # Unlike an interrupted run, a cancelled one waits for its next scheduled time.
    assert scheduler.next_due(Job("pair:a", ())) == clock.now + scheduler.settings.interval_seconds


def test_sigterm_without_a_cancel_is_an_interrupted_run(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "status.json", Recorder(code=5), Clock())
    assert scheduler.run(Job("pair:a", ())).result == "killed"


def test_a_request_waits_while_its_lane_is_busy_and_can_be_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeward.daemon import scheduler as module
    from homeward.runtime import Paths, drop_request, pending_requests

    monkeypatch.setattr(module, "MAX_PARALLEL_RUNS", 1)
    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    config = _config(tmp_path, pairs=("a", "b"))
    release = threading.Event()
    runner = StoppableRunner(lambda: release.wait(5))
    clock = Clock()
    daemon = Daemon(config, Scheduler(tmp_path / "status.json", runner, clock), runner)
    assert daemon.reload()
    _request(tmp_path, "a", '{"pair": "a"}')
    _request(tmp_path, "b", '{"pair": "b"}')

    daemon.run_requests()
    assert [r.pair for r in pending_requests(paths)] == ["b"]  # still shown as queued
    assert drop_request(paths, "b")  # the user cancels it before it starts
    release.set()
    daemon.scheduler.wait()

    assert runner.calls == ["pair:a"]


def test_a_run_waiting_for_icloud_is_postponed_not_failed(tmp_path: Path) -> None:
    from homeward.runtime import Paths, read_events

    sent: list[str] = []
    scheduler = Scheduler(
        tmp_path / "status.json", Recorder(code=4), Clock(), notifier=lambda e, *a: sent.append(e)
    )
    status = scheduler.run(Job("photos:me", ("photos",)))
    assert status.result == "waiting"
    (event, _started) = read_events(Paths(var_dir=tmp_path, config=tmp_path / "config.toml"))
    assert (event.level, event.code) == ("info", "sync_postponed")
    assert sent == []


def test_the_log_rotates_with_the_output_of_the_runs(tmp_path: Path) -> None:
    # In a separate process: the handler points fd 1 and 2 at the log file.
    script = """
import logging, subprocess, sys
from pathlib import Path
from homeward.daemon.scheduler import StdioRotatingHandler
log_file = Path(sys.argv[1])
logging.basicConfig(level=logging.INFO, handlers=[StdioRotatingHandler(log_file, 400, 2)])
for i in range(30):
    logging.info("daemon line %02d", i)
    subprocess.run([sys.executable, "-c", f"print('run output {i:02d}')"], check=True)
"""
    log_file = tmp_path / "homeward.log"
    subprocess.run([sys.executable, "-c", script, str(log_file)], check=True)

    files = sorted(tmp_path.glob("homeward.log*"))
    assert [f.name for f in files] == ["homeward.log", "homeward.log.1", "homeward.log.2"]
    assert all(f.stat().st_size < 600 for f in files)
    current = log_file.read_text()
    assert "daemon line 29" in current
    assert "run output 29" in current


def test_jobs_of_different_apple_ids_run_side_by_side(tmp_path: Path) -> None:
    release = threading.Event()
    started: list[str] = []

    def runner(job: Job) -> int:
        started.append(job.name)
        release.wait(5)
        return 0

    scheduler = Scheduler(tmp_path / "status.json", runner, Clock())
    photos_me = Job("photos:me", ("photos",), account="me")
    drive_me = Job("pair:docs", ("sync",), account="me")
    photos_anna = Job("photos:anna", ("photos",), account="anna")
    folder_pair = Job("pair:local", ("sync",))
    scheduler.jobs = [photos_me, drive_me, photos_anna, folder_pair]

    assert scheduler.start_due() == [photos_me, photos_anna]  # one per Apple ID, two at most
    assert scheduler.due() == [drive_me, folder_pair]
    assert scheduler.seconds_until_next() == MAX_SLEEP_SECONDS  # woken when a run ends
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["running_jobs"] == ["photos:anna", "photos:me"]
    release.set()
    scheduler.wait()
    assert sorted(started) == ["photos:anna", "photos:me"]
    assert scheduler.start_due() == [drive_me, folder_pair]
    scheduler.wait()
    assert scheduler.running == set()


def test_a_job_without_success_for_days_warns_once(tmp_path: Path) -> None:
    from homeward.daemon.scheduler import STALE_DAYS

    sent: list[tuple[str, ...]] = []
    clock = Clock()
    runner = Recorder(code=0)
    scheduler = Scheduler(
        tmp_path / "status.json", runner, clock, notifier=lambda e, *a: sent.append((e, *a))
    )
    job, other = Job("photos:fotos", ("photos",)), Job("pair:docs", ("sync",))
    scheduler.jobs = [job, other]
    scheduler.run(job)
    assert scheduler.status[job.name].last_success == clock.now
    assert scheduler.check_stale() == []  # the other job never ran: nothing to judge yet

    runner.code = 1  # from now on every run fails
    clock.now += 86400
    scheduler.run(job)
    scheduler.run(Job("photos:fotos", ("photos", "--dry-run")))  # a dry run is no backup
    clock.now += (STALE_DAYS - 1) * 86400
    assert scheduler.check_stale() == [job]
    assert scheduler.check_stale() == []  # once only
    assert sent[-1] == ("not_backed_up", "fotos", str(STALE_DAYS))

    # the flag and the last success survive a restart; a success re-arms the warning
    restarted = Scheduler(tmp_path / "status.json", runner, clock, notifier=lambda e, *a: True)
    restarted.jobs = [job]
    assert restarted.status[job.name].stale_notified
    runner.code = 0
    restarted.run(job)
    assert not restarted.status[job.name].stale_notified
    assert restarted.status[job.name].last_success == clock.now


def test_a_job_that_never_succeeded_warns_after_days(tmp_path: Path) -> None:
    from homeward.daemon.scheduler import STALE_DAYS

    clock = Clock()
    scheduler = Scheduler(
        tmp_path / "status.json", Recorder(code=6), clock, notifier=lambda *a: True
    )
    job = Job("pair:docs", ("sync",))
    scheduler.jobs = [job]
    scheduler.run(job)
    clock.now += STALE_DAYS * 86400
    scheduler.run(job)
    assert scheduler.status[job.name].tracked_since == 1_000_000.0
    assert scheduler.check_stale() == [job]


def test_a_time_window_limits_scheduled_runs(tmp_path: Path) -> None:
    from datetime import datetime

    from homeward.core.limits import TimeWindow

    def at(hour: int, day: int = 15) -> float:
        return datetime(2026, 10, day, hour).timestamp()

    clock = Clock()
    clock.now = at(12)
    release = threading.Event()
    codes = {"photos:night": -15, "pair:docs": 0}

    def runner(job: Job) -> int:
        release.wait(5)
        return codes[job.name]

    scheduler = Scheduler(tmp_path / "status.json", runner, clock, notifier=lambda *a: True)
    night = Job("photos:night", ("photos",), account="a", window=TimeWindow.parse("22:00-06:00"))
    now = Job("pair:docs", ("sync",), window=TimeWindow.parse("22:00-06:00"))
    scheduler.jobs = [night, now]
    assert scheduler.due() == [] and scheduler.next_due(night) == at(22)

    clock.now = at(23)
    assert scheduler.start_due() == [night, now]
    clock.now = at(5)
    assert scheduler.window_closed() == []  # still open
    clock.now = at(6, day=16)
    assert sorted(scheduler.window_closed()) == ["docs", "photos:night"]
    assert scheduler.window_closed() == []  # already stopping
    release.set()
    scheduler.wait()
    assert scheduler.status["photos:night"].result == "window_closed"
    assert scheduler.status["pair:docs"].result == "ok"  # finished before the stop landed
    assert scheduler.next_due(night) == at(22, day=16)  # goes on when the window opens

    # "sync now" ignores the window and is not stopped when it closes
    release.clear()
    assert scheduler.start(now)
    assert scheduler.window_closed() == []
    release.set()
    scheduler.wait()
