"""Command line interface."""

import argparse
import contextlib
import fcntl
import getpass
import logging
import os
import sys
from collections.abc import Iterator
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
from homeward.providers.icloud_photos import ICloudPhotosSource
from homeward.providers.nas import NasProvider

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


def _remote_provider(pair: PairConfig) -> Provider:
    if pair.remote_provider == "folder":
        return NasProvider(Path(pair.remote), name="remote")
    raise ConfigError(
        f"pair {pair.name!r}: the iCloud Drive provider is not available yet; "
        'use remote_provider = "folder" to try the sync core'
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


def _cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pairs = [config.pair(args.pair)] if args.pair else list(config.pairs)
    if not pairs:
        raise ConfigError("no sync pairs configured")
    status = EXIT_OK
    for pair in pairs:
        remote = _remote_provider(pair)
        local = NasProvider(pair.local, name="nas")
        with StateDB(config.state_path(pair.account)) as state:
            outcome = sync_pair(
                pair, local, remote, state, dry_run=args.dry_run, allow_deletes=args.allow_deletes
            )
        _print_outcome(outcome, args.dry_run)
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
        state_path = config.photo_state_path(job.account)
        with _run_lock(state_path.with_suffix(".lock")) as locked:
            if not locked:
                print(
                    f"homeward: another photos run for account {job.account} is still running",
                    file=sys.stderr,
                )
                status = max(status, EXIT_FAILED)
                continue
            log.info("photos %s: reading iCloud (this can take a few minutes)", job.name)
            try:
                source = _photo_source(config, job)
            except LoginRequired as exc:
                print(f"homeward: {exc}", file=sys.stderr)
                status = max(status, EXIT_FAILED)
                continue
            with PhotoStateDB(state_path) as state:
                outcome = backup_photos(
                    job.name, source, job.target, state, job.layout,
                    albums=job.albums or job.favorites, dry_run=args.dry_run, workers=job.workers,
                )  # fmt: skip
        _print_photos(job, outcome, args.dry_run)
        if outcome.result and outcome.result.failed:
            status = max(status, EXIT_FAILED)
    return status


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
        apple_id = _apple_id(jobs[0]) if jobs else None
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
