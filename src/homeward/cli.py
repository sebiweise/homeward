"""Command line interface."""

import argparse
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from homeward.core.config import DEFAULT_CONFIG_PATH, ConfigError, PairConfig, load_config
from homeward.core.models import ActionKind, SyncMode
from homeward.core.provider import Provider
from homeward.core.state import StateDB
from homeward.core.sync import SyncOutcome, sync_pair
from homeward.providers.nas import NasProvider

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeward", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="sync the configured pairs")
    sync.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("HOMEWARD_CONFIG", DEFAULT_CONFIG_PATH)),
        help="config file (default: $HOMEWARD_CONFIG or %(default)s)",
    )
    sync.add_argument("--pair", help="only sync this pair")
    sync.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sync.add_argument(
        "--allow-deletes", action="store_true", help="approve a run the delete brake stopped"
    )

    args = parser.parse_args(argv)
    if args.command != "sync":
        parser.print_help()
        return EXIT_OK
    try:
        return _cmd_sync(args)
    except ConfigError as exc:
        print(f"homeward: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
