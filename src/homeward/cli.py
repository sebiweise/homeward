"""Command line interface."""

import argparse
import contextlib
import fcntl
import functools
import getpass
import logging
import os
import signal
import sys
from collections.abc import Callable, Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType

from homeward.auth.session import LoginRequired, open_service, session_rejected
from homeward.auth.twofa import LoginError, LoginFlow
from homeward.core.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    PairConfig,
    PhotosConfig,
    load_config,
)
from homeward.core.models import ActionKind, FolderMissing, SyncMode
from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import PhotoActionKind, PhotoOutcome, PhotoSource, backup_photos
from homeward.core.progress import Progress, ProgressLogHandler
from homeward.core.provider import Provider
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.core.trash import Trash
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
from homeward.providers.drive_api import DriveApi, PyiCloudDriveApi
from homeward.providers.icloud_drive import FolderCache, ICloudDriveProvider
from homeward.providers.icloud_photos import ICloudPhotosSource, PhotoOptions, PhotosNotReady
from homeward.providers.nas import NasProvider, find_recycle_bin
from homeward.runtime import EventLogHandler, Paths, clear_progress, write_progress

# Named explicitly: the daemon runs this module as __main__ (python -m homeward.cli),
# and only loggers below "homeward" reach the event log.
log = logging.getLogger("homeward.cli")

# how bad an exit code is, to report the worst one when a command runs several jobs
_SEVERITY = {
    EXIT_OK: 0, EXIT_WAITING: 1, EXIT_FAILED: 2, EXIT_LOGIN: 3, EXIT_BLOCKED: 4, EXIT_USAGE: 5,
}  # fmt: skip


def _worst(a: int, b: int) -> int:
    return max(a, b, key=lambda code: _SEVERITY.get(code, 5))


class Cancelled(BaseException):
    """SIGTERM arrived (the user cancelled the run, or the service stops).

    A BaseException, so the ``except Exception`` of a single step does not swallow it:
    the run ends at once, and every ``finally`` and ``with`` on the way out still runs
    (temporary files are removed, locks and databases closed). Each finished step is
    already in the state database, so the next run continues from there.
    """


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    # Only the first signal ends the run; a second one must not cut the cleanup short.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Cancelled


def _version() -> str:
    try:
        return version("homeward")
    except PackageNotFoundError:
        return "0.0.0"


def _icloud_drive_api(pair: PairConfig, config: Config) -> DriveApi:
    """Sign in with the account's stored trusted session (see ``homeward login``)."""
    service = open_service(pair.account, _pair_apple_id(pair), config.state_dir)
    return PyiCloudDriveApi(service.drive)


def _log_login_required(exc: LoginRequired, account: str) -> None:
    """An expected state, not a crash: the event log gets the message, no traceback."""
    log.error("%s", exc, extra={"event": "login_required", "account": account})


def _pair_apple_id(pair: PairConfig) -> str:
    if pair.apple_id:
        return pair.apple_id
    if "@" in pair.account:
        return pair.account
    raise ConfigError(f"pair {pair.name!r}: set apple_id (or use the Apple ID as account)")


# Returns a signed-in Drive API for the pair's account; tests replace it.
drive_api_factory: Callable[[PairConfig, Config], DriveApi] = _icloud_drive_api
# Sends a DSM notification (see homeward.dsm.notify); tests replace it.
notifier: Callable[..., bool] = notify


def _remote_provider(pair: PairConfig, config: Config) -> Provider:
    if pair.remote_provider == "folder":
        remote = Path(pair.remote)
        return NasProvider(remote, name="remote", recycle_bin=find_recycle_bin(remote))
    cache = FolderCache(config.state_dir / f"{pair.account}.drive-folders.json")
    return ICloudDriveProvider(
        drive_api_factory(pair, config), pair.remote, excludes=pair.exclude, cache=cache
    )


def _print_outcome(outcome: SyncOutcome, dry_run: bool) -> None:
    plan = outcome.plan
    beta = ", beta" if plan.mode is SyncMode.BIDIRECTIONAL else ""
    title = f"Pair {plan.pair} ({plan.mode}{beta})"
    if plan.first_run:
        title += ", first run: no deletes, no overwrites"
    print(title)
    for action in plan.actions:
        line = f"  {action.kind:<14} {action.path}"
        if action.conflict_path:
            line += f" -> {action.conflict_path}"
        if action.renamed_to:
            line += f" -> {action.renamed_to}"
        print(f"{line}  [{action.reason}]")
    counts = plan.counts()
    summary = ", ".join(f"{counts[kind]} {kind}" for kind in ActionKind if kind in counts)
    if plan.renames:
        summary += f"; {plan.renames} of the deletes are renames, the delete brake skips them"
    print(f"  {summary or 'nothing to do'}")
    if plan.blocked and (outcome.result is None or outcome.result.blocked):
        print(f"  Delete brake: {plan.blocked}. Re-run with --allow-deletes to approve.")
    if dry_run:
        print("  Dry run: nothing was changed.")
    elif outcome.result:
        for failure in outcome.result.failed:
            print(f"  FAILED {failure.action.kind} {failure.action.path}: {failure.error}")


@contextlib.contextmanager
def _events(config_path: Path, source: str) -> Iterator[None]:
    """Record this run's waits, retries and errors in the event log next to the config."""
    handler = EventLogHandler(source, Paths(var_dir=config_path.parent, config=config_path))
    logger = logging.getLogger("homeward")
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


@contextlib.contextmanager
def _progress(config_path: Path, source: str) -> Iterator[Progress]:
    """Keep ``progress/<source>.json`` next to the config up to date while the run lasts,
    with the latest log line as the current activity."""
    paths = Paths(var_dir=config_path.parent, config=config_path)
    progress = Progress(functools.partial(write_progress, paths, source))
    handler = ProgressLogHandler(progress)
    logger = logging.getLogger("homeward")
    logger.addHandler(handler)
    try:
        yield progress
    finally:
        logger.removeHandler(handler)
        try:
            clear_progress(paths, source)
        except OSError as exc:
            log.debug("could not remove the progress file: %s", exc)


def _reported(run: Callable[[], int], account: str) -> int:
    """Run one pair or job; an error that ends it becomes a readable event, not only
    a traceback in the log. When Apple rejects the account's session mid-run, the
    account is marked as needing a new sign-in."""
    try:
        return run()
    except ConfigError:
        raise
    except LoginRequired as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        _log_login_required(exc, exc.account)
        return EXIT_LOGIN
    except PhotosNotReady as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        log.warning("%s", exc, extra={"event": "icloud_indexing"})
        return EXIT_WAITING
    except FolderMissing as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        log.error("%s", exc, extra={"event": "folder_missing"})
        return EXIT_FAILED
    except Exception as exc:
        if session_rejected(exc):
            required = LoginRequired(account, _describe(exc))
            print(f"homeward: {required}", file=sys.stderr)
            _log_login_required(required, account)
            return EXIT_LOGIN
        else:
            log.exception("the run stopped: %s", _describe(exc))
        return EXIT_FAILED


def _describe(exc: BaseException) -> str:
    """The error and what caused it, e.g. "Photos service not available (iCloud Photo
    Library not finished indexing)"."""
    text = str(exc) or type(exc).__name__
    cause = exc.__cause__ or exc.__context__
    if cause is not None and str(cause) and str(cause) not in text:
        text += f" ({cause})"
    return text


def _cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pairs = [config.pair(args.pair)] if args.pair else list(config.pairs)
    if not pairs:
        raise ConfigError("no sync pairs configured")
    status = EXIT_OK
    for pair in pairs:
        source = f"pair:{pair.name}"
        with _events(args.config, source), _progress(args.config, source) as progress:
            run = functools.partial(_sync_one, args, config, pair, progress)
            status = _worst(status, _reported(run, pair.account))
    return status


def _sync_one(
    args: argparse.Namespace, config: Config, pair: PairConfig, progress: Progress
) -> int:
    # Also keeps a manual `homeward sync` apart from the daemon's run of the same pair.
    with _run_lock(config.state_dir / f"pair-{pair.name}.lock") as locked:
        if not locked:
            message = f"another sync of pair {pair.name} is still running"
            print(f"homeward: {message}", file=sys.stderr)
            log.warning("%s", message, extra={"event": "already_running"})
            return EXIT_FAILED
        return _sync_locked(args, config, pair, progress)


def _sync_locked(
    args: argparse.Namespace, config: Config, pair: PairConfig, progress: Progress
) -> int:
    status = EXIT_OK
    progress.phase("connecting")
    remote = _remote_provider(pair, config)
    local = NasProvider(pair.local, name="nas", recycle_bin=find_recycle_bin(pair.local))
    try:
        with StateDB(config.state_path(pair.account)) as state:
            outcome = sync_pair(
                pair, local, remote, state, dry_run=args.dry_run,
                allow_deletes=args.allow_deletes, progress=progress,
            )  # fmt: skip
    except (FolderMissing, LoginRequired):
        raise  # reported on their own, without a traceback
    except Exception as exc:  # e.g. iCloud unreachable while listing: try the next pair
        if session_rejected(exc):
            raise  # _reported asks for a new sign-in
        print(f"Pair {pair.name}: FAILED: {type(exc).__name__}: {exc}")
        log.exception("%s: %s", type(exc).__name__, exc, extra={"event": "sync_failed"})
        return EXIT_FAILED
    _print_outcome(outcome, args.dry_run)
    if outcome.result:
        conflicts = sum(1 for a in outcome.result.done if a.kind is ActionKind.CONFLICT)
        if conflicts:
            notifier(Event.CONFLICTS, pair.name, str(conflicts))
    failed = len(outcome.result.failed) if outcome.result else 0
    counts = {str(kind): n for kind, n in outcome.plan.counts().items()}
    _log_summary(f"pair {pair.name}", counts, failed, _pair_files(outcome))
    if not args.dry_run:
        purged = local.purge_trash(pair.trash_days)
        if isinstance(remote, NasProvider):  # a "folder" pair has a NAS trash on both sides
            purged += remote.purge_trash(pair.trash_days)
        if purged:
            print(f"  Emptied {purged} day folder(s) older than {pair.trash_days} days "
                  "from the NAS trash.")  # fmt: skip
    if outcome.plan.blocked and (outcome.result is None or outcome.result.blocked):
        status = max(status, EXIT_BLOCKED)
    elif outcome.result and outcome.result.failed:
        status = max(status, EXIT_FAILED)
    return status


def _apple_id(job: PhotosConfig) -> str:
    if job.apple_id:
        return job.apple_id
    if "@" in job.account:
        return job.account
    raise ConfigError(f"photos {job.name!r}: set apple_id")


class _StderrHandler(logging.Handler):
    """Writes progress to whatever ``sys.stderr`` is at the time (works with redirects)."""

    def emit(self, record: logging.LogRecord) -> None:
        print(self.format(record), file=sys.stderr, flush=True)


def _setup_logging() -> None:
    logger = logging.getLogger("homeward")
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _photo_source(config: Config, job: PhotosConfig, progress: Progress) -> PhotoSource:
    service = open_service(job.account, _apple_id(job), config.state_dir)
    return ICloudPhotosSource(
        service,
        job.libraries,
        PhotoOptions(
            albums=job.albums,
            videos=job.videos,
            live_videos=job.live_videos,
            raw_alternative=job.raw_alternative,
            edited=job.edited,
            favorites=job.favorites,
            hidden=job.hidden,
            xmp=job.xmp,
            album_names=job.album_names,
        ),
        progress=progress,
    )


def _print_photos(job: PhotosConfig, outcome: PhotoOutcome, dry_run: bool) -> None:
    plan = outcome.plan
    print(f"Photos {job.name} ({job.account}) -> {job.target}")
    if plan.unchanged:
        print(f"  nothing changed in iCloud, {plan.up_to_date} photos up to date")
        return
    for action in plan.actions:
        line = f"  {action.kind:<9} {action.path}"
        if action.target:
            line += f" -> {action.target}"
        print(line)
    counts = plan.counts()
    summary = ", ".join(f"{n} {kind}" for kind, n in counts.items())
    print(f"  {summary or 'nothing to do'}; {plan.up_to_date} up to date")
    if plan.blocked:
        print(f"  Delete brake: {plan.blocked}. Re-run with --allow-deletes to approve.")
    if outcome.integrity is not None:
        check = outcome.integrity
        print(f"  Weekly check: {check.checked} photos checked, {len(check.damaged)} damaged"
              + " (downloaded again if iCloud still has them)" * bool(check.damaged))  # fmt: skip
    if dry_run:
        print("  Dry run: nothing was changed.")
    elif outcome.result:
        for failure in outcome.result.failed:
            print(f"  FAILED {failure.action.kind} {failure.action.path}: {failure.error}")


def _cmd_photos(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    jobs = [config.photos_job(args.job)] if args.job else list(config.photos)
    if not jobs:
        raise ConfigError("no photos jobs configured")
    status = EXIT_OK
    for job in jobs:
        source = f"photos:{job.name}"
        with _events(args.config, source), _progress(args.config, source) as progress:
            run = functools.partial(_photos_one, args, config, job, progress)
            status = _worst(status, _reported(run, job.account))
    return status


def _photos_one(
    args: argparse.Namespace, config: Config, job: PhotosConfig, progress: Progress
) -> int:
    state_path = config.photo_state_path(job.account)
    with _run_lock(state_path.with_suffix(".lock")) as locked:
        if not locked:
            message = f"another photos run for account {job.account} is still running"
            print(f"homeward: {message}", file=sys.stderr)
            log.warning("%s", message, extra={"event": "already_running"})
            return EXIT_FAILED
        progress.phase("connecting")
        log.info("photos %s: reading iCloud (this can take a few minutes)", job.name)
        source = _photo_source(config, job, progress)  # LoginRequired: see _reported
        with PhotoStateDB(state_path) as state:
            outcome = backup_photos(
                job.name, source, job.target, state, job.layout,
                albums=job.albums or job.favorites, dry_run=args.dry_run, workers=job.workers,
                progress=progress, mirror_deletes=job.mirror_deletes, selection=_selection(job),
                brake=job.delete_brake, allow_deletes=args.allow_deletes,
                remove_gone=args.remove_gone, recycle_bin=find_recycle_bin(job.target),
                max_rate_kb=job.max_rate_kb,
            )  # fmt: skip
    _print_photos(job, outcome, args.dry_run)
    failed = len(outcome.result.failed) if outcome.result else 0
    counts = {str(kind): n for kind, n in outcome.plan.counts().items()}
    counts["up_to_date"] = outcome.plan.up_to_date
    _log_summary(f"photos {job.name}", counts, failed, _photo_files(outcome))
    if not args.dry_run:
        # Also without mirror_deletes: replaced files land there too. Only the
        # backup's own trash: DSM empties #recycle on its own schedule.
        purged = Trash(job.target).purge(job.trash_days)
        if purged:
            print(f"  Emptied {purged} day folder(s) older than {job.trash_days} days "
                  "from the NAS trash.")  # fmt: skip
    if outcome.result and outcome.result.failed:
        return EXIT_FAILED
    if outcome.result and outcome.result.blocked:
        return EXIT_BLOCKED
    return EXIT_OK


def _selection(job: PhotosConfig) -> str:
    """What decides which files a photos job backs up; deletions are only mirrored
    while it stays the same (see ``build_photo_plan``)."""
    libraries = ",".join(sorted(lib.value for lib in job.libraries))
    switches = (job.videos, job.live_videos, job.raw_alternative, job.edited, job.xmp, job.hidden)
    return f"{libraries}:" + "".join("1" if on else "0" for on in switches)


# The run details in the UI list the files a run removed, replaced or kept twice.
MAX_LISTED_FILES = 200


def _log_summary(
    what: str, counts: dict[str, int], failed: int, files: list[dict[str, str]] | None = None
) -> None:
    """Record what a run did (planned, for a dry run), so the DSM UI can show it."""
    counts = {kind: n for kind, n in counts.items() if n}
    if failed:
        counts["failed"] = failed
    text = ", ".join(f"{n} {kind}" for kind, n in counts.items()) or "nothing to do"
    extra: dict[str, object] = {"event": "sync_summary", "counts": counts}
    if files:
        extra["files"] = files[:MAX_LISTED_FILES]
        extra["files_total"] = len(files)
    log.info("%s: %s", what, text, extra=extra)


def _pair_files(outcome: SyncOutcome) -> list[dict[str, str]]:
    """Deleted, replaced and conflicting files of a pair's run (planned in a dry run)."""
    actions = outcome.plan.actions if outcome.result is None else outcome.result.done
    files = []
    for action in actions:
        if action.kind in (ActionKind.DELETE_LOCAL, ActionKind.DELETE_REMOTE):
            kind = "renamed" if action.renamed_to else str(action.kind)
            files.append({"kind": kind, "path": action.path, "to": action.renamed_to or ""})
        elif action.kind is ActionKind.CONFLICT:
            copy = action.conflict_path or ""
            files.append({"kind": "conflict", "path": action.path, "to": copy})
        elif action.overwrite and action.kind in (ActionKind.UPLOAD, ActionKind.DOWNLOAD):
            side = "replaced-remote" if action.kind is ActionKind.UPLOAD else "replaced-local"
            files.append({"kind": side, "path": action.path, "to": ""})
    return files


def _photo_files(outcome: PhotoOutcome) -> list[dict[str, str]]:
    """Deleted, replaced and moved photos of a run (planned in a dry run)."""
    actions = outcome.plan.actions if outcome.result is None else outcome.result.done
    kinds = {PhotoActionKind.DELETE: "delete", PhotoActionKind.REPLACE: "replace"}
    files = []
    for action in actions:
        if action.kind is PhotoActionKind.MOVE:
            files.append({"kind": "move", "path": action.target or "", "to": action.path})
        elif action.kind in kinds:
            files.append({"kind": kinds[action.kind], "path": action.path, "to": ""})
    return files


@contextlib.contextmanager
def _run_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock for one account's run; yields False if another run has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cmd_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    apple_id = args.apple_id
    if not apple_id:
        jobs = [job for job in config.photos if job.account == args.account]
        pairs = [p for p in config.pairs if p.account == args.account and p.apple_id]
        if jobs:
            apple_id = _apple_id(jobs[0])
        elif pairs:
            apple_id = pairs[0].apple_id
    if not apple_id:
        raise ConfigError(f"no apple_id known for account {args.account!r}: pass --apple-id")
    password = os.environ.get("HOMEWARD_PASSWORD") or getpass.getpass("Apple ID password: ")
    flow = LoginFlow(args.account, apple_id, config.state_dir)
    try:
        if flow.login(password):
            flow.verify_2fa(input("2FA code from your Apple device: "))
    except LoginError as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Account {args.account} ({apple_id}) is logged in.")
    return EXIT_OK


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("HOMEWARD_CONFIG", DEFAULT_CONFIG_PATH)),
        help="config file (default: $HOMEWARD_CONFIG or %(default)s)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeward", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="sync the configured pairs")
    _add_config_arg(sync)
    sync.add_argument("--pair", help="only sync this pair")
    sync.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sync.add_argument(
        "--allow-deletes", action="store_true", help="approve a run the delete brake stopped"
    )

    photos = sub.add_parser("photos", help="back up iCloud Photos to the NAS")
    _add_config_arg(photos)
    photos.add_argument("--job", help="only run this photos job")
    photos.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    photos.add_argument(
        "--allow-deletes", action="store_true", help="approve deletions the delete brake held back"
    )
    photos.add_argument(
        "--remove-gone",
        action="store_true",
        help="also delete the photos earlier runs found deleted in iCloud (to the trash)",
    )

    login = sub.add_parser("login", help="log in to an Apple ID (password and 2FA)")
    _add_config_arg(login)
    login.add_argument("--account", required=True, help="account name used in the config")
    login.add_argument("--apple-id", help="Apple ID (default: from the account's photos job)")

    args = parser.parse_args(argv)
    _setup_logging()
    commands = {"sync": _cmd_sync, "photos": _cmd_photos, "login": _cmd_login}
    if args.command not in commands:
        parser.print_help()
        return EXIT_OK
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return commands[args.command](args)
    except ConfigError as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Cancelled:
        print("homeward: cancelled, finished steps are kept", file=sys.stderr)
        return EXIT_CANCELLED
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
