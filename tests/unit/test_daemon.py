import json
from collections.abc import Callable
from pathlib import Path

import pytest

from homeward.core.config import ConfigError
from homeward.daemon.scheduler import (
    Daemon,
    Job,
    Scheduler,
    Settings,
    SubprocessRunner,
    load_plan,
    main,
    parse_settings,
    take_requests,
)


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

    daemon.cycle()  # missing file
    assert not daemon.config_ok
    assert recorder.calls == []
    _config(tmp_path)
    daemon.cycle()
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
    daemon.cycle()  # scheduled run
    assert len(runner.argv) == 1
    scheduled_at = scheduler.status["pair:a"].started_at

    _request(tmp_path, "a", '{"pair": "a", "dry_run": true}')
    _request(tmp_path, "zzz", '{"pair": "missing"}')
    daemon.cycle()
    assert runner.argv[1][-1] == "--dry-run"
    assert len(runner.argv) == 2
    # A dry run does not count as the scheduled run.
    assert scheduler.status["pair:a"].started_at == scheduled_at

    clock.now += 60
    _request(tmp_path, "a", '{"pair": "a", "allow_deletes": true}')
    daemon.cycle()
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
    daemon.cycle()
    _request(tmp_path, "p", '{"pair": "photos:me", "dry_run": true, "allow_deletes": true}')
    daemon.cycle()
    assert runner.argv[-1] == (
        "photos", "--config", str(config), "--job", "me", "--dry-run", "--allow-deletes"
    )  # fmt: skip
    _request(tmp_path, "p", '{"pair": "photos:me", "remove_gone": true}')
    daemon.cycle()
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

    def terminate(self, timeout: float = 0.0) -> None:
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
    daemon.cycle()

    assert runner.terminated == 1
    status = scheduler.status["pair:a"]
    assert (status.result, status.exit_code) == ("cancelled", 5)
    assert scheduler.running is None and scheduler.cancelled is None
    assert not list((tmp_path / "cancel").glob("*.json"))
    (event, _started) = read_events(paths)
    assert (event.level, event.code, event.source) == ("warning", "sync_cancelled", "pair:a")
    assert sent == []  # cancelling is not an error worth a notification
    # Unlike an interrupted run, a cancelled one waits for its next scheduled time.
    assert scheduler.next_due(Job("pair:a", ())) == clock.now + scheduler.settings.interval_seconds


def test_sigterm_without_a_cancel_is_an_interrupted_run(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "status.json", Recorder(code=5), Clock())
    assert scheduler.run(Job("pair:a", ())).result == "killed"


def test_cancelling_a_request_the_daemon_already_took(tmp_path: Path) -> None:
    from homeward.runtime import CancelRequest, Paths, read_events, request_cancel

    paths = Paths(var_dir=tmp_path, config=tmp_path / "config.toml")
    config = _config(tmp_path, pairs=("a", "b"))
    clock = Clock()
    runner = StoppableRunner(lambda: request_cancel(paths, CancelRequest("b")))
    daemon = Daemon(config, Scheduler(tmp_path / "status.json", runner, clock), runner)
    daemon.cycle()  # the scheduled runs of a and b
    runner.calls.clear()
    clock.now += 60
    request_cancel(paths, CancelRequest("a"))  # stale: from before the requests below

    _request(tmp_path, "a", '{"pair": "a"}')
    _request(tmp_path, "b", '{"pair": "b"}')
    daemon.cycle()  # a runs and, while it does, the user cancels the queued b
    assert runner.calls == ["pair:a"]
    assert runner.terminated == 0
    assert read_events(paths)[0].code == "sync_cancelled"


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
