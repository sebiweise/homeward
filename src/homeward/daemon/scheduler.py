"""Interval-based sync scheduler.

The daemon runs every sync pair and photos job once per interval. Each run is its
own ``homeward sync --pair NAME`` (or ``homeward photos --job NAME``) process: a
crash, or the memory a large transfer needs, ends with the run, and the
long-running daemon itself stays small.

Signals: SIGTERM/SIGINT stop the daemon (a running sync is terminated; the
executor records every finished step, so the next run picks up the rest),
SIGUSR1 runs every job now. The config file is re-read before every cycle.

"Sync now" requests from the DSM UI are files ``requests/<name>.json`` next to the
config (``{"pair": ..., "dry_run": ..., "allow_deletes": ...}``, where ``pair`` is a
pair name or ``photos:<job>``); the daemon removes each one and runs it.
``status.json`` holds ``running`` (the pair, or ``photos:<job>``, being synced) and
the last result and next run of every job.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from homeward.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

log = logging.getLogger("homeward.daemon")

DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5
# Upper bound for one sleep, so config changes and run requests are picked up
# without a signal.
MAX_SLEEP_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 30.0

# Exit codes of `homeward sync` (see homeward.cli).
_RESULTS = {0: "ok", 1: "failed", 2: "config_error", 3: "blocked"}


@dataclass(frozen=True, slots=True)
class Job:
    """One scheduled command, e.g. the sync of one pair."""

    name: str
    argv: tuple[str, ...]


@dataclass(slots=True)
class JobStatus:
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    result: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0


@dataclass(frozen=True, slots=True)
class RunRequest:
    """A "sync now" request for one pair, written by the DSM UI."""

    pair: str
    dry_run: bool = False
    allow_deletes: bool = False


def take_requests(requests_dir: Path) -> list[RunRequest]:
    """Return and remove the pending requests; unreadable files are dropped."""
    if not requests_dir.is_dir():
        return []
    requests = []
    for file in sorted(requests_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            requests.append(
                RunRequest(
                    pair=str(data["pair"]),
                    dry_run=bool(data.get("dry_run", False)),
                    allow_deletes=bool(data.get("allow_deletes", False)),
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring run request %s: %s", file.name, exc)
        file.unlink(missing_ok=True)
    return requests


@dataclass(slots=True)
class Plan:
    settings: Settings
    jobs: list[Job] = field(default_factory=list)


def load_plan(config_path: Path) -> Plan:
    """Read the ``[daemon]`` table and turn every sync pair and photos job into a job."""
    config = load_config(config_path)
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    settings = parse_settings(data.get("daemon", {}))
    jobs = [
        Job(f"pair:{pair.name}", ("sync", "--config", str(config_path), "--pair", pair.name))
        for pair in config.pairs
    ]
    jobs += [
        Job(f"photos:{photos.name}", ("photos", "--config", str(config_path), "--job", photos.name))
        for photos in config.photos
    ]
    return Plan(settings, jobs)


def parse_settings(table: object) -> Settings:
    if not isinstance(table, dict):
        raise ConfigError("'daemon' must be a table")
    interval = table.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise ConfigError("daemon: 'interval_minutes' must be an integer")
    if interval < MIN_INTERVAL_MINUTES:
        raise ConfigError(f"daemon: 'interval_minutes' must be at least {MIN_INTERVAL_MINUTES}")
    return Settings(interval_minutes=interval)


class Scheduler:
    """Decides which jobs are due and keeps their status in a JSON file."""

    def __init__(
        self,
        status_path: Path,
        runner: Callable[[Job], int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.status_path = status_path
        self._runner = runner
        self._clock = clock
        self.settings = Settings()
        self.jobs: list[Job] = []
        self.status: dict[str, JobStatus] = self._load_status()
        self._forced: set[str] = set()
        self.running: str | None = None

    def update(self, plan: Plan) -> None:
        self.settings = plan.settings
        self.jobs = list(plan.jobs)

    def next_due(self, job: Job) -> float:
        if job.name in self._forced:
            return 0.0
        status = self.status.get(job.name, JobStatus())
        if status.started_at is None or status.finished_at is None or status.result == "killed":
            return 0.0  # never ran, or a stop or crash interrupted the last run
        return status.started_at + self.settings.interval_seconds

    def due(self) -> list[Job]:
        now = self._clock()
        return [job for job in self.jobs if self.next_due(job) <= now]

    def seconds_until_next(self) -> float:
        if not self.jobs:
            return MAX_SLEEP_SECONDS
        wait = min(self.next_due(job) for job in self.jobs) - self._clock()
        return max(0.0, min(wait, MAX_SLEEP_SECONDS))

    def run(self, job: Job, *, record: bool = True) -> JobStatus:
        """Run one job; ``record=False`` (dry runs) leaves its schedule alone."""
        status = JobStatus(started_at=self._clock())
        if record:
            self._forced.discard(job.name)
            self.status[job.name] = status
        self.running = job.name.removeprefix("pair:")
        self.save()
        log.info("starting %s", job.name)
        try:
            code = self._runner(job)
        except OSError as exc:
            log.error("%s could not start: %s", job.name, exc)
            code = 127
        status.finished_at = self._clock()
        status.exit_code = code
        status.result = _RESULTS.get(code, "killed" if code < 0 else "failed")
        log.info("finished %s: %s (exit %d)", job.name, status.result, code)
        self.running = None
        self.save()
        return status

    def run_due(self, stop: threading.Event | None = None) -> list[Job]:
        ran = []
        for job in self.due():
            if stop is not None and stop.is_set():
                break
            self.run(job)
            ran.append(job)
        return ran

    def run_all_now(self) -> None:
        """Make every job due on the next cycle."""
        self._forced.update(job.name for job in self.jobs)

    def save(self) -> None:
        names = {job.name for job in self.jobs}
        data: dict[str, Any] = {
            "updated_at": self._clock(),
            "pid": os.getpid(),
            "interval_minutes": self.settings.interval_minutes,
            "running": self.running,
            "jobs": {
                name: {**asdict(status), "next_due": self._next_due_by_name(name)}
                for name, status in self.status.items()
                if name in names
            },
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_name(self.status_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.status_path)

    def _next_due_by_name(self, name: str) -> float | None:
        started = self.status[name].started_at
        if name in self._forced or started is None:
            return None
        return started + self.settings.interval_seconds

    def _load_status(self) -> dict[str, JobStatus]:
        try:
            data = json.loads(self.status_path.read_text())
            jobs = data["jobs"]
            return {
                name: JobStatus(
                    started_at=entry.get("started_at"),
                    finished_at=entry.get("finished_at"),
                    exit_code=entry.get("exit_code"),
                    result=entry.get("result"),
                )
                for name, entry in jobs.items()
            }
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("ignoring unreadable status file %s: %s", self.status_path, exc)
            return {}


class SubprocessRunner:
    """Runs a job as ``python -m homeward.cli ARGV`` and can terminate it."""

    def __init__(self, python: str = sys.executable) -> None:
        self._python = python
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def __call__(self, job: Job) -> int:
        with self._lock:
            self._proc = subprocess.Popen([self._python, "-m", "homeward.cli", *job.argv])
        try:
            return self._proc.wait()
        finally:
            with self._lock:
                self._proc = None

    def terminate(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        log.info("stopping the running sync (pid %d)", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()


class Daemon:
    def __init__(self, config_path: Path, scheduler: Scheduler, runner: SubprocessRunner) -> None:
        self.config_path = config_path
        self.requests_dir = config_path.parent / "requests"
        self.scheduler = scheduler
        self.runner = runner
        self.stop = threading.Event()
        self._wake = threading.Event()
        self._run_now = False
        self._config_error: str | None = None

    @property
    def config_ok(self) -> bool:
        return self._config_error is None

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)
        signal.signal(signal.SIGUSR1, self._on_run_now)

    def _on_stop(self, signum: int, frame: FrameType | None) -> None:
        log.info("received signal %d, stopping", signum)
        self.stop.set()
        self._wake.set()
        # The main thread is blocked waiting for the sync process; end it here.
        threading.Thread(target=self.runner.terminate, daemon=True).start()

    def _on_run_now(self, signum: int, frame: FrameType | None) -> None:
        self._run_now = True
        self._wake.set()

    def reload(self) -> bool:
        try:
            plan = load_plan(self.config_path)
        except ConfigError as exc:
            message = str(exc)
            if message != self._config_error:
                log.error("config: %s", message)
                self._config_error = message
            return False
        if self._config_error is not None:
            log.info("config is valid again")
            self._config_error = None
        self.scheduler.update(plan)
        return True

    def cycle(self) -> None:
        if self.reload():
            if self._run_now:
                self._run_now = False
                self.scheduler.run_all_now()
            self.run_requests()
            self.scheduler.run_due(self.stop)
            self.scheduler.save()

    def run_requests(self) -> None:
        jobs = {job.name: job for job in self.scheduler.jobs}
        for request in take_requests(self.requests_dir):
            if self.stop.is_set():
                break
            # Requests name a pair, or a photos job as "photos:<name>".
            name = request.pair if ":" in request.pair else f"pair:{request.pair}"
            job = jobs.get(name)
            if job is None:
                log.warning("run request for unknown job %r ignored", request.pair)
                continue
            extra = ("--dry-run",) * request.dry_run
            if request.allow_deletes and name.startswith("pair:"):
                extra += ("--allow-deletes",)
            log.info("run requested for %s%s", job.name, " (dry run)" if request.dry_run else "")
            self.scheduler.run(Job(job.name, job.argv + extra), record=not request.dry_run)

    def run_forever(self) -> None:
        log.info("daemon started (pid %d), config %s", os.getpid(), self.config_path)
        while not self.stop.is_set():
            self.cycle()
            if self.stop.is_set():
                break
            wait = self.scheduler.seconds_until_next() if self.config_ok else MAX_SLEEP_SECONDS
            self._wake.wait(wait)
            self._wake.clear()
        log.info("daemon stopped")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeward-daemon", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("HOMEWARD_CONFIG", DEFAULT_CONFIG_PATH)),
        help="config file (default: $HOMEWARD_CONFIG or %(default)s)",
    )
    parser.add_argument(
        "--status", type=Path, help="status file (default: status.json next to the config)"
    )
    parser.add_argument("--once", action="store_true", help="run the due jobs once and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    status_path = args.status or args.config.parent / "status.json"
    runner = SubprocessRunner()
    daemon = Daemon(args.config, Scheduler(status_path, runner), runner)
    if args.once:
        daemon.cycle()
        return 0 if daemon.config_ok else 2
    daemon.install_signal_handlers()
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
