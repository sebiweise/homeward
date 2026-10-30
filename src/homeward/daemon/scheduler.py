"""Interval-based sync scheduler.

The daemon runs every sync pair and photos job once per interval: ``[daemon]
interval_minutes`` for all jobs, unless a pair or photos job sets its own. Each run is its
own ``homeward sync --pair NAME`` (or ``homeward photos --job NAME``) process: a
crash, or the memory a large transfer needs, ends with the run, and the
long-running daemon itself stays small.

Signals: SIGTERM/SIGINT stop the daemon (a running sync is terminated; the
executor records every finished step, so the next run picks up the rest),
SIGUSR1 runs every job now. The config file is re-read before every cycle.

"Cancel" requests from the DSM UI are files ``cancel/<name>.json``: a watcher
thread stops the running sync of that pair the same way (SIGTERM, the sync ends
cleanly after removing its temporary files), or drops it from the queue. A
cancelled job keeps its normal schedule and sends no notification.

"Sync now" requests from the DSM UI are files ``requests/<name>.json`` next to the
config (``{"pair": ..., "dry_run": ..., "allow_deletes": ...}``, where ``pair`` is a
pair name or ``photos:<job>``); the daemon removes each one and runs it.
``status.json`` holds ``running`` (the pair, or ``photos:<job>``, being synced) and
the last result and next run of every job.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Any

from homeward.core.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_INTERVAL_MINUTES,
    ConfigError,
    check_interval,
    load_config,
)
from homeward.core.limits import TimeWindow
from homeward.dsm.notify import Event, notify
from homeward.exitcodes import (
    EXIT_BLOCKED,
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_LOGIN,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_WAITING,
)
from homeward.runtime import (
    Level,
    Paths,
    RunRequest,
    record_event,
    take_cancels,
    take_requests,
)

log = logging.getLogger("homeward.daemon")

# Upper bound for one sleep, so config changes and run requests are picked up
# without a signal.
MAX_SLEEP_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 30.0
CANCEL_POLL_SECONDS = 1.0
# Jobs of different Apple IDs (or without one) run side by side, at most this many.
MAX_PARALLEL_RUNS = 2
# A run iCloud was not ready for (e.g. still indexing) is tried again this soon,
# instead of after the whole interval.
WAITING_RETRY_MINUTES = 15
# A job without a successful run for this long warns in the UI and notifies DSM once.
STALE_DAYS = 3

# homeward.log is rotated at this size, keeping this many old files (homeward.log.1 ...).
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

# Exit codes of `homeward sync` (see homeward.exitcodes) -> run result.
_RESULTS = {
    EXIT_OK: "ok", EXIT_FAILED: "failed", EXIT_USAGE: "config_error", EXIT_BLOCKED: "blocked",
    EXIT_WAITING: "waiting", EXIT_CANCELLED: "killed", EXIT_LOGIN: "login_required",
}  # fmt: skip
# result -> event level and code for the event log shown in the DSM UI
_EVENTS = {
    "ok": (Level.INFO, "sync_finished"),
    "blocked": (Level.WARNING, "delete_brake"),
    "config_error": (Level.ERROR, "config_error"),
    "killed": (Level.WARNING, "sync_stopped"),
    "cancelled": (Level.WARNING, "sync_cancelled"),
    "window_closed": (Level.INFO, "sync_paused"),
    "waiting": (Level.INFO, "sync_postponed"),
    "login_required": (Level.ERROR, "sync_failed"),
}
_MESSAGES = {
    "killed": "stopped, runs again shortly",
    "waiting": f"iCloud is not ready yet, trying again in {WAITING_RETRY_MINUTES} minutes",
    "cancelled": "cancelled by the user, finished steps are kept",
    "window_closed": "the time window closed, finished steps are kept; goes on when it opens",
    "login_required": "the Apple ID needs a new sign-in",
}
# results that send a DSM notification when they first occur
_NOTIFY = {"failed": Event.SYNC_FAILED, "blocked": Event.DELETE_BRAKE}


@dataclass(frozen=True, slots=True)
class Job:
    """One scheduled command, e.g. the sync of one pair."""

    name: str
    argv: tuple[str, ...]
    interval_minutes: int | None = None  # None: the global interval
    account: str = ""  # the Apple ID account the job signs in with
    window: TimeWindow | None = None  # scheduled runs start only in these hours


@dataclass(slots=True)
class JobStatus:
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    result: str | None = None
    last_success: float | None = None  # end of the last successful (real) run
    tracked_since: float | None = None  # start of the first run, until one succeeds
    stale_notified: bool = False  # the "not backed up" notification went out


@dataclass(frozen=True, slots=True)
class Settings:
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0


@dataclass(slots=True)
class Plan:
    settings: Settings
    jobs: list[Job] = field(default_factory=list)


def load_plan(config_path: Path) -> Plan:
    """Read the interval and turn every sync pair and photos job into a job."""
    config = load_config(config_path)
    settings = Settings(interval_minutes=config.interval_minutes)
    jobs = [
        Job(
            f"pair:{pair.name}",
            ("sync", "--config", str(config_path), "--pair", pair.name),
            pair.interval_minutes,
            pair.account if pair.remote_provider == "icloud_drive" else "",
            pair.window,
        )
        for pair in config.pairs
    ]
    jobs += [
        Job(
            f"photos:{photos.name}",
            ("photos", "--config", str(config_path), "--job", photos.name),
            photos.interval_minutes,
            photos.account,
            photos.window,
        )
        for photos in config.photos
    ]
    return Plan(settings, jobs)


def parse_settings(table: object) -> Settings:
    if not isinstance(table, dict):
        raise ConfigError("'daemon' must be a table")
    interval = table.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    return Settings(interval_minutes=check_interval(interval, "daemon"))


class Scheduler:
    """Decides which jobs are due and keeps their status in a JSON file."""

    def __init__(
        self,
        status_path: Path,
        runner: Callable[[Job], int],
        clock: Callable[[], float] = time.time,
        notifier: Callable[..., bool] = notify,
    ) -> None:
        self.status_path = status_path
        self._runner = runner
        self._clock = clock
        self._notify = notifier
        var_dir = status_path.parent
        self.events = Paths(var_dir=var_dir, config=var_dir / "config.toml")
        self.settings = Settings()
        self.jobs: list[Job] = []
        self.status: dict[str, JobStatus] = self._load_status()
        self._forced: set[str] = set()
        # The running jobs, as the UI names them: the pair name, or ``photos:<job>``.
        self.running: set[str] = set()
        # The running jobs (in the format of ``running``) the user asked to stop.
        self.cancelled: set[str] = set()
        # The running jobs the schedule started (not a "sync now"), and those of
        # them stopped because their time window closed.
        self.scheduled: set[str] = set()
        self.closing: set[str] = set()
        self._lock = threading.RLock()
        self._lanes: set[str] = set()  # see ``lane``
        self._threads: list[threading.Thread] = []
        self.on_finish: Callable[[], None] = lambda: None  # called when a run ends

    def update(self, plan: Plan) -> None:
        self.settings = plan.settings
        self.jobs = list(plan.jobs)

    def interval_minutes(self, job: Job) -> int:
        return job.interval_minutes or self.settings.interval_minutes

    def next_due(self, job: Job) -> float:
        if job.name in self._forced:
            return 0.0  # "run all now" ignores the time window, as "sync now" does
        status = self.status.get(job.name, JobStatus())
        if (
            status.started_at is None
            or status.finished_at is None
            or status.result
            in (
                "killed",
                "window_closed",
            )
        ):
            due = 0.0  # never ran, or a stop or crash interrupted the last run
        else:
            due = status.started_at + self._minutes_after(job, status) * 60.0
        if job.window is not None:
            due = job.window.next_open(max(due, self._clock()))
        return due

    def window_closed(self) -> list[str]:
        """The running jobs (as in ``running``) the schedule started that must stop
        now that their time window has closed; marks them as closing. A "sync now"
        is left alone."""
        now = self._clock()
        result = []
        with self._lock:
            for job in self.jobs:
                name = _running_name(job)
                if (
                    job.window is not None
                    and name in self.scheduled
                    and name not in self.cancelled | self.closing
                    and not job.window.contains(now)
                ):
                    self.closing.add(name)
                    result.append(name)
        return result

    def due(self) -> list[Job]:
        """The jobs whose time has come and that do not run right now."""
        now = self._clock()
        with self._lock:
            running = set(self.running)
        return [
            job for job in self.jobs
            if _running_name(job) not in running and self.next_due(job) <= now
        ]  # fmt: skip

    def seconds_until_next(self) -> float:
        """How long to sleep until the next job is due. A due job that waits for its
        lane is left out: the end of a run wakes the daemon (``on_finish``)."""
        now = self._clock()
        with self._lock:
            running = set(self.running)
        waits = []
        for job in self.jobs:
            if _running_name(job) in running:
                continue
            due = self.next_due(job)
            if due <= now and not self.can_start(job):
                continue
            waits.append(due - now)
        if not waits:
            return MAX_SLEEP_SECONDS
        return max(0.0, min(min(waits), MAX_SLEEP_SECONDS))

    def lane(self, job: Job) -> str:
        """Jobs in the same lane never run at the same time: those of one Apple ID
        (Apple dislikes parallel sessions of one account), else each job alone."""
        return f"account:{job.account}" if job.account else job.name

    def can_start(self, job: Job) -> bool:
        with self._lock:
            return (
                self.lane(job) not in self._lanes
                and len(self._lanes) < MAX_PARALLEL_RUNS
                and _running_name(job) not in self.running
            )

    def run(self, job: Job, *, record: bool = True, scheduled: bool = False) -> JobStatus:
        """Run one job and wait for it; ``record=False`` (dry runs) leaves its
        schedule alone, ``scheduled`` runs stop when their time window closes."""
        lane = self.lane(job)
        with self._lock:
            self._lanes.add(lane)
        status, previous = self._begin(job, record, scheduled)
        try:
            self._execute(job, status, previous)
        finally:
            with self._lock:
                self._lanes.discard(lane)
        return status

    def start(self, job: Job, *, record: bool = True, scheduled: bool = False) -> bool:
        """Start one job on its own thread, unless its lane is busy or
        ``MAX_PARALLEL_RUNS`` already run. Returns whether it started."""
        lane = self.lane(job)
        with self._lock:
            if not self.can_start(job):
                return False
            self._lanes.add(lane)
            status, previous = self._begin(job, record, scheduled)

        def work() -> None:
            try:
                self._execute(job, status, previous)
            except Exception:  # never let one job's bookkeeping stop the daemon
                log.exception("running %s failed", job.name)
            finally:
                with self._lock:
                    self._lanes.discard(lane)
                self.on_finish()

        thread = threading.Thread(target=work, name=f"run-{job.name}", daemon=True)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return True

    def start_due(self, stop: threading.Event | None = None) -> list[Job]:
        """Start every due job whose lane is free; the others wait for a later cycle."""
        started = []
        for job in self.due():
            if stop is not None and stop.is_set():
                break
            if self.start(job, scheduled=True):
                started.append(job)
        return started

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the jobs started with ``start``."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._lanes)

    def _begin(self, job: Job, record: bool, scheduled: bool) -> tuple[JobStatus, str | None]:
        """Mark the job as running; returns its new status and its previous result."""
        now = self._clock()
        name = _running_name(job)
        with self._lock:
            old = self.status.get(job.name, JobStatus())
            previous = old.result
            status = JobStatus(
                started_at=now,
                last_success=old.last_success,
                tracked_since=old.tracked_since or now,
                stale_notified=old.stale_notified,
            )
            if record:
                self._forced.discard(job.name)
                self.status[job.name] = status
            self.running.add(name)
            if scheduled:
                self.scheduled.add(name)
            self.cancelled.discard(name)
            self.save()
        dry_run = "--dry-run" in job.argv
        record_event(Level.INFO, job.name, "sync_started", paths=self.events, dry_run=dry_run)
        log.info("starting %s", job.name)
        return status, previous

    def _execute(self, job: Job, status: JobStatus, previous: str | None) -> None:
        name = _running_name(job)
        dry_run = "--dry-run" in job.argv
        try:
            code = self._runner(job)
        except OSError as exc:
            log.exception("%s could not start: %s", job.name, exc)
            code = 127
        with self._lock:
            status.finished_at = self._clock()
            status.exit_code = code
            status.result = _RESULTS.get(code, "killed" if code < 0 else "failed")
            if status.result == "killed" and name in self.cancelled:
                status.result = "cancelled"
            elif status.result == "killed" and name in self.closing:
                status.result = "window_closed"
            if status.result == "ok" and not dry_run:
                status.last_success = status.finished_at
                status.stale_notified = False
            notify_login = status.result == "login_required" and self._first_login_needed(
                job, previous
            )
        log.info("finished %s: %s (exit %d)", job.name, status.result, code)
        level, event = _EVENTS.get(status.result, (Level.ERROR, "sync_failed"))
        message = _MESSAGES.get(status.result or "", f"exit code {code}")
        record_event(level, job.name, event, message, paths=self.events,
                     dry_run=dry_run, exit_code=code)  # fmt: skip
        if status.result in _NOTIFY and status.result != previous and not dry_run:
            self._notify(_NOTIFY[status.result], job.name.split(":", 1)[-1])
        elif notify_login:
            self._notify(Event.LOGIN_NEEDED, job.account or job.name.split(":", 1)[-1])
        with self._lock:
            self.running.discard(name)
            self.cancelled.discard(name)
            self.closing.discard(name)
            self.scheduled.discard(name)
            self.save()

    def check_stale(self) -> list[Job]:
        """Warn once about every job without a successful run for ``STALE_DAYS``
        (or, never successful, since its first run); returns the jobs warned about."""
        now = self._clock()
        warned = []
        with self._lock:
            for job in self.jobs:
                status = self.status.get(job.name)
                since = status and (status.last_success or status.tracked_since)
                if not status or not since or status.stale_notified:
                    continue
                days = int((now - since) // 86400)
                if days < STALE_DAYS:
                    continue
                status.stale_notified = True
                warned.append((job, days))
            if warned:
                self._save()
        for job, days in warned:
            log.warning("%s has not been backed up for %d days", job.name, days)
            record_event(Level.WARNING, job.name, "not_backed_up",
                         f"no successful run for {days} days", paths=self.events,
                         days=days)  # fmt: skip
            self._notify(Event.NOT_BACKED_UP, job.name.split(":", 1)[-1], str(days))
        return [job for job, _days in warned]

    def _first_login_needed(self, job: Job, previous: str | None) -> bool:
        """Notify once per account: not again while this or another job of the same
        Apple ID still waits for the new sign-in."""
        if previous == "login_required":
            return False
        return not any(
            other.name != job.name
            and other.account == job.account
            and self.status.get(other.name, JobStatus()).result == "login_required"
            for other in self.jobs
        )

    def run_due(self, stop: threading.Event | None = None) -> list[Job]:
        """Run the due jobs one after the other (``--once``, and tests)."""
        ran = []
        for job in self.due():
            if stop is not None and stop.is_set():
                break
            self.run(job, scheduled=True)
            ran.append(job)
        return ran

    def run_all_now(self) -> None:
        """Make every job due on the next cycle."""
        self._forced.update(job.name for job in self.jobs)

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        jobs = {job.name: job for job in self.jobs}
        running = sorted(self.running)
        data: dict[str, Any] = {
            "updated_at": self._clock(),
            "pid": os.getpid(),
            "interval_minutes": self.settings.interval_minutes,
            # ``running`` (one job) is kept for UIs from before parallel runs.
            "running": running[0] if running else None,
            "running_jobs": running,
            "jobs": {
                name: {
                    **asdict(status),
                    "next_due": self._next_due_in_status(jobs[name]),
                    "interval_minutes": self.interval_minutes(jobs[name]),
                }
                for name, status in self.status.items()
                if name in jobs
            },
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_name(self.status_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.status_path)

    def _minutes_after(self, job: Job, status: JobStatus) -> int:
        interval = self.interval_minutes(job)
        return min(interval, WAITING_RETRY_MINUTES) if status.result == "waiting" else interval

    def _next_due_in_status(self, job: Job) -> float | None:
        status = self.status[job.name]
        if job.name in self._forced or status.started_at is None:
            return None
        due = status.started_at + self._minutes_after(job, status) * 60.0
        if job.window is not None:
            due = job.window.next_open(max(due, self._clock()))
        return due

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
                    last_success=entry.get("last_success"),
                    tracked_since=entry.get("tracked_since"),
                    stale_notified=bool(entry.get("stale_notified", False)),
                )
                for name, entry in jobs.items()
            }
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("ignoring unreadable status file %s: %s", self.status_path, exc)
            return {}


def _running_name(job: Job) -> str:
    """The job as ``running`` and the UI name it: the pair name, or ``photos:<job>``."""
    return job.name.removeprefix("pair:")


def _job_name(running: str) -> str:
    return running if ":" in running else f"pair:{running}"


class SubprocessRunner:
    """Runs jobs as ``python -m homeward.cli ARGV`` (several at once) and can
    terminate them."""

    def __init__(self, python: str = sys.executable) -> None:
        self._python = python
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    def __call__(self, job: Job) -> int:
        with self._lock:
            proc = subprocess.Popen([self._python, "-m", "homeward.cli", *job.argv])
            self._procs[job.name] = proc
        try:
            return proc.wait()
        finally:
            with self._lock:
                self._procs.pop(job.name, None)

    def terminate(self, name: str | None = None, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Stop the run of the job ``name`` (``pair:x``, ``photos:y``), or all runs."""
        with self._lock:
            procs = [p for n, p in self._procs.items() if name is None or n == name]
        running = [proc for proc in procs if proc.poll() is None]
        for proc in running:
            log.info("stopping the running sync (pid %d)", proc.pid)
            proc.terminate()
        for proc in running:
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()


class Daemon:
    def __init__(self, config_path: Path, scheduler: Scheduler, runner: SubprocessRunner) -> None:
        self.config_path = config_path
        self.requests_dir = config_path.parent / "requests"
        self.scheduler = scheduler
        scheduler.on_finish = self._wake_up
        self.runner = runner
        self.stop = threading.Event()
        self._wake = threading.Event()
        self._run_now = False
        self._config_error: str | None = None
        self.paths = scheduler.events
        # Queued "sync now" requests the user cancelled before they started.
        self._dropped: set[str] = set()
        self._cancel_lock = threading.Lock()

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

    def _wake_up(self) -> None:
        self._wake.set()

    def _on_run_now(self, signum: int, frame: FrameType | None) -> None:
        self._run_now = True
        self._wake.set()

    def check_cancels(self) -> None:
        """Act on cancel requests: stop the running sync, or drop a queued request.
        Also stops the scheduled runs whose time window has closed."""
        with self._cancel_lock:
            for name in self.scheduler.window_closed():
                log.info("time window of %s closed, stopping it", name)
                self.runner.terminate(_job_name(name))
            for request in take_cancels(self.paths):
                name = request.pair
                if name in self.scheduler.running:
                    if name in self.scheduler.cancelled:
                        continue  # already stopping
                    log.info("cancel requested for %s, stopping it", name)
                    self.scheduler.cancelled.add(name)
                    self.runner.terminate(_job_name(name))
                else:
                    self._dropped.add(request.pair)

    def _watch_cancels(self) -> None:
        while not self.stop.wait(CANCEL_POLL_SECONDS):
            try:
                self.check_cancels()
            except Exception:  # the watcher must keep running
                log.exception("checking cancel requests failed")

    def reload(self) -> bool:
        try:
            plan = load_plan(self.config_path)
        except ConfigError as exc:
            message = str(exc)
            if message != self._config_error:
                log.error("config: %s", message)
                record_event(Level.ERROR, "service", "config_error", message,
                             paths=self.scheduler.events)  # fmt: skip
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
            self.scheduler.start_due(self.stop)
            self.scheduler.check_stale()
            self.scheduler.save()

    def run_requests(self) -> None:
        """Start the "sync now" requests whose Apple ID is free; the others stay
        queued until a later cycle."""
        self.check_cancels()
        self._dropped.clear()  # cancels from before these requests were taken
        take_requests(self.requests_dir, self._start_request)

    def _start_request(self, request: RunRequest) -> bool:
        """Start one request; False leaves it queued."""
        if self.stop.is_set():
            return False
        jobs = {job.name: job for job in self.scheduler.jobs}
        # Requests name a pair, or a photos job as "photos:<name>".
        name = _job_name(request.pair)
        self.check_cancels()
        if request.pair in self._dropped:
            self._dropped.discard(request.pair)
            log.info("run request for %s cancelled before it started", name)
            record_event(Level.WARNING, name, "sync_cancelled",
                         "cancelled by the user before it started",
                         paths=self.paths)  # fmt: skip
            return True
        job = jobs.get(name)
        if job is None:
            log.warning("run request for unknown job %r ignored", request.pair)
            return True
        extra = ("--dry-run",) * request.dry_run
        if request.allow_deletes:
            extra += ("--allow-deletes",)
        if request.remove_gone and name.startswith("photos:"):
            extra += ("--remove-gone",)
        run = replace(job, argv=job.argv + extra)
        if not self.scheduler.start(run, record=not request.dry_run):
            return False
        log.info("run requested for %s%s", job.name, " (dry run)" if request.dry_run else "")
        return True

    def run_forever(self) -> None:
        log.info("daemon started (pid %d), config %s", os.getpid(), self.config_path)
        threading.Thread(target=self._watch_cancels, name="cancel-watcher", daemon=True).start()
        while not self.stop.is_set():
            self.cycle()
            if self.stop.is_set():
                break
            wait = self.scheduler.seconds_until_next() if self.config_ok else MAX_SLEEP_SECONDS
            self._wake.wait(wait)
            self._wake.clear()
        self.runner.terminate()
        self.scheduler.wait(STOP_TIMEOUT_SECONDS)
        log.info("daemon stopped")


class StdioRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotates the log file while the daemon runs and keeps stdout and stderr on it.

    The runs are child processes that write straight to the inherited stdout and
    stderr; pointing those at the current file after every rollover means their
    output rotates with the daemon's own (a size check sees all of it).
    """

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self._redirect()

    def doRollover(self) -> None:
        super().doRollover()
        self._redirect()

    def _redirect(self) -> None:
        if self.stream is None:
            self.stream = self._open()
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in (1, 2):
            os.dup2(self.stream.fileno(), fd)


def _setup_logging(log_file: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler: logging.Handler = (
        StdioRotatingHandler(log_file) if log_file else logging.StreamHandler(sys.stdout)
    )
    handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler])


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
    parser.add_argument(
        "--log-file",
        type=Path,
        help="log here, rotated at 5 MB (3 old files); also takes stdout and stderr",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_file)
    status_path = args.status or args.config.parent / "status.json"
    runner = SubprocessRunner()
    daemon = Daemon(args.config, Scheduler(status_path, runner), runner)
    if args.once:
        daemon.cycle()
        daemon.scheduler.wait()
        return 0 if daemon.config_ok else 2
    daemon.install_signal_handlers()
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
