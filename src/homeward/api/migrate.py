"""Package lifecycle steps both DSM packages share (``package/scripts`` and spksrc).

    python -m homeward.api.migrate            # after an upgrade: update the config
    python -m homeward.api.migrate --init     # after the install: default config
    python -m homeward.api.migrate --backup SHARE   # before an uninstall

Paths come from ``$HOMEWARD_VAR`` and ``$HOMEWARD_CONFIG`` (``Paths.from_env``).
"""

import argparse
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from homeward.api.store import ConfigStore
from homeward.runtime import Paths

DEFAULT_CONFIG = """\
# Homeward configuration. Add accounts, photos backups and sync pairs in the
# Homeward app in DSM. All options:
# https://github.com/sebiweise/homeward/blob/master/docs/user-guide.md
state_dir = "state"

[daemon]
interval_minutes = 60
"""
BACKUP_DIR = ".homeward-backup"
# Never copied into a share: anyone who can read it could use them to reach the Apple ID.
SESSION_DIRS = ("sessions", "sessions-pending")


def init(paths: Paths) -> bool:
    """Write the default config unless there is one; returns whether it wrote it."""
    (paths.var_dir / "state").mkdir(parents=True, exist_ok=True)
    if paths.config.exists():
        return False
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    with paths.config.open("x", encoding="utf-8") as fh:
        fh.write(DEFAULT_CONFIG)
    return True


def backup(paths: Paths, share: Path, now: float | None = None) -> Path | None:
    """Copy the config and the state (without the iCloud sessions) into ``share``
    before DSM deletes var/, so a reinstall can pick them up again. The synced files
    live in shares and are never touched. None when there is nothing to save."""
    if not share.is_dir() or not paths.config.is_file():
        return None
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(now))
    dest = share / BACKUP_DIR / stamp
    dest.mkdir(parents=True)
    shutil.copy2(paths.config, dest / paths.config.name)
    state = paths.var_dir / "state"
    if state.is_dir():
        shutil.copytree(state, dest / "state", symlinks=True, ignore=_without_sessions(state))
    return dest


def _without_sessions(state: Path) -> Callable[[str, list[str]], list[str]]:
    def ignore(folder: str, names: list[str]) -> list[str]:
        return [n for n in names if n in SESSION_DIRS] if Path(folder) == state else []

    return ignore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m homeward.api.migrate")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="write the default config")
    group.add_argument("--backup", type=Path, metavar="SHARE", help="back up into SHARE")
    args = parser.parse_args(argv)
    paths = Paths.from_env()
    if args.init:
        if init(paths):
            print(f"Writing the default configuration to {paths.config}")
        return 0
    if args.backup is not None:
        try:
            dest = backup(paths, args.backup)
        except OSError as exc:
            print(f"Could not save the configuration and state: {exc}", file=sys.stderr)
            return 1
        if dest is not None:
            print(f"Saved configuration and state (without the iCloud sessions) to {dest}")
        return 0
    for name in ConfigStore(paths).migrate():
        print(f"Pair {name}: set beta = true (bidirectional sync is in beta)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
