"""Command line interface."""

import argparse
import contextlib
import fcntl
import getpass
import logging
import os
import sys
from collections.abc import Callable, Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from homeward.auth.session import LoginRequired, open_service
from homeward.auth.twofa import LoginError, LoginFlow
from homeward.core.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    PairConfig,
    PhotosConfig,
    load_config,
)
from homeward.core.models import ActionKind
from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos import PhotoOutcome, PhotoSource, backup_photos
from homeward.core.provider import Provider
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.providers.drive_api import DriveApi, PyiCloudDriveApi
from homeward.providers.icloud_drive import FolderCache, ICloudDriveProvider
from homeward.providers.icloud_photos import ICloudPhotosSource
from homeward.providers.nas import NasProvider, find_recycle_bin
from homeward.runtime import EventLogHandler, Paths

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3


def _version() -> str:
    try:
        return version("homeward")
    except PackageNotFoundError:
        return "0.0.0"


def _icloud_drive_api(pair: PairConfig, config: Config) -> DriveApi:
    """Sign in with the account's stored trusted session (see ``homeward login``)."""
    try:
        service = open_service(pair.account, _pair_apple_id(pair), config.state_dir)
    except LoginRequired as exc:
        log.error("%s", exc, extra={"event": "login_required", "account": pair.account})
        raise ConfigError(f"pair {pair.name!r}: {exc}") from exc
    return PyiCloudDriveApi(service.drive)


def _pair_apple_id(pair: PairConfig) -> str:
    if pair.apple_id:
        return pair.apple_id
    if "@" in pair.account:
        return pair.account
    raise ConfigError(f"pair {pair.name!r}: set apple_id (or use the Apple ID as account)")


# Returns a signed-in Drive API for the pair's account; tests replace it.
drive_api_factory: Callable[[PairConfig, Config], DriveApi] = _icloud_drive_api


def _remote_provider(pair: PairConfig, config: Config) -> Provider:
    if pair.remote_provider == "folder":
        return NasProvider(Path(pair.remote), name="remote")
    cache = FolderCache(config.state_dir / f"{pair.account}.drive-folders.json")
    return ICloudDriveProvider(
        drive_api_factory(pair, config), pair.remote, excludes=pair.exclude, cache=cache
    )


def _print_outcome(outcome: SyncOutcome, dry_run: bool) -> None:
    plan = outcome.plan
    title = f"Pair {plan.pair} ({plan.mode})"
    if plan.first_run:
        title += ", first run: no deletes, no overwrites"
    print(title)
    for action in plan.actions:
        line = f"  {action.kind:<14} {action.path}"
        if action.conflict_path:
            line += f" -> {action.conflict_path}"
        print(f"{line}  [{action.reason}]")
    counts = plan.counts()
    summary = ", ".join(f"{counts[kind]} {kind}" for kind in ActionKind if kind in counts)
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


def _cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pairs = [config.pair(args.pair)] if args.pair else list(config.pairs)
    if not pairs:
        raise ConfigError("no sync pairs configured")
    status = EXIT_OK
    for pair in pairs:
        with _events(args.config, f"pair:{pair.name}"):
            status = max(status, _sync_one(args, config, pair))
    return status


def _sync_one(args: argparse.Namespace, config: Config, pair: PairConfig) -> int:
    status = EXIT_OK
    remote = _remote_provider(pair, config)
    local = NasProvider(pair.local, name="nas", recycle_bin=find_recycle_bin(pair.local))
    try:
        with StateDB(config.state_path(pair.account)) as state:
            outcome = sync_pair(
                pair, local, remote, state, dry_run=args.dry_run,
                allow_deletes=args.allow_deletes,
            )  # fmt: skip
    except Exception as exc:  # e.g. iCloud unreachable while listing: try the next pair
        print(f"Pair {pair.name}: FAILED: {type(exc).__name__}: {exc}")
        log.error("%s: %s", type(exc).__name__, exc, extra={"event": "sync_failed"})
        return EXIT_FAILED
    _print_outcome(outcome, args.dry_run)
    if not args.dry_run:
        purged = local.purge_trash(pair.trash_days)
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


def _photo_source(config: Config, job: PhotosConfig) -> PhotoSource:
    service = open_service(job.account, _apple_id(job), config.state_dir)
    return ICloudPhotosSource(
        service,
        job.libraries,
        albums=job.albums,
        videos=job.videos,
        live_videos=job.live_videos,
        raw_alternative=job.raw_alternative,
        edited=job.edited,
        favorites=job.favorites,
        hidden=job.hidden,
        xmp=job.xmp,
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
        with _events(args.config, f"photos:{job.name}"):
            status = max(status, _photos_one(args, config, job))
    return status


def _photos_one(args: argparse.Namespace, config: Config, job: PhotosConfig) -> int:
    state_path = config.photo_state_path(job.account)
    with _run_lock(state_path.with_suffix(".lock")) as locked:
        if not locked:
            message = f"another photos run for account {job.account} is still running"
            print(f"homeward: {message}", file=sys.stderr)
            log.warning("%s", message, extra={"event": "already_running"})
            return EXIT_FAILED
        log.info("photos %s: reading iCloud (this can take a few minutes)", job.name)
        try:
            source = _photo_source(config, job)
        except LoginRequired as exc:
            print(f"homeward: {exc}", file=sys.stderr)
            log.error("%s", exc, extra={"event": "login_required", "account": job.account})
            return EXIT_FAILED
        with PhotoStateDB(state_path) as state:
            outcome = backup_photos(
                job.name, source, job.target, state, job.layout,
                albums=job.albums or job.favorites, dry_run=args.dry_run, workers=job.workers,
            )  # fmt: skip
    _print_photos(job, outcome, args.dry_run)
    if outcome.result and outcome.result.failed:
        return EXIT_FAILED
    return EXIT_OK


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
    try:
        return commands[args.command](args)
    except ConfigError as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
