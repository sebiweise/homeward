import json
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
    for bad in ({"interval_minutes": 1}, {"interval_minutes": "5"}, {"interval_minutes": True}):
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


def test_status_survives_a_restart(tmp_path: Path) -> None:
    clock, runner = Clock(), Recorder(code=3)
    plan = load_plan(_config(tmp_path))
    first = _scheduler(tmp_path, runner, clock)
    first.update(plan)
    first.run_due()

    status = json.loads((tmp_path / "status.json").read_text())
    job = status["jobs"]["pair:docs"]
    assert job["result"] == "blocked" and job["exit_code"] == 3
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
    assert status.result == "failed" and status.exit_code == 127


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
    assert not daemon.config_ok and recorder.calls == []
    _config(tmp_path)
    daemon.cycle()
    assert daemon.config_ok and recorder.calls == ["pair:docs"]


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
    assert runner.argv[-1] == ("photos", "--config", str(config), "--job", "me", "--dry-run")
