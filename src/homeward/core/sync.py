"""One sync run for one pair: list both sides, plan, and (unless dry run) execute."""

import logging
import time
from dataclasses import dataclass
from datetime import date

from homeward.core.config import PairConfig
from homeward.core.executor import Executor, RunResult
from homeward.core.limits import throttle_for
from homeward.core.models import FileMeta, FileRecord, FolderMissing, Plan
from homeward.core.planner import build_plan
from homeward.core.progress import Progress
from homeward.core.provider import Provider
from homeward.core.state import StateDB

log = logging.getLogger("homeward.core.sync")


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    plan: Plan
    result: RunResult | None  # None for a dry run


def sync_pair(
    pair: PairConfig,
    local: Provider,
    remote: Provider,
    state: StateDB,
    *,
    dry_run: bool,
    allow_deletes: bool = False,
    today: date | None = None,
    progress: Progress | None = None,
) -> SyncOutcome:
    started = time.time()
    progress = progress or Progress()
    progress.phase("listing")
    local_files = local.list_files()
    remote_files = remote.list_files()
    records = _records(pair, state, dry_run)
    if records:
        _check_present(local, local_files, f"NAS folder {pair.local}", len(records))
        _check_present(remote, remote_files, f"remote folder {pair.remote}", len(records))
    progress.phase("planning")
    plan = build_plan(
        pair.name,
        pair.mode,
        local_files,
        remote_files,
        records,
        excludes=pair.exclude,
        brake=pair.delete_brake,
        today=today,
        remote_case_insensitive=getattr(remote, "case_insensitive", False),
        remote_identity=getattr(remote, "file_identity", None),
    )
    result = None
    if not dry_run:
        throttle = throttle_for(pair.max_rate_kb)
        executor = Executor(local, remote, state, pair.name, progress, throttle)
        result = executor.run(plan, allow_deletes=allow_deletes)
    state.log_run(
        pair.name,
        started,
        dry_run=dry_run,
        actions=len(plan.actions),
        errors=len(result.failed) if result else 0,
        blocked=plan.blocked if result is None or result.blocked else None,
    )
    return SyncOutcome(plan, result)


def _records(pair: PairConfig, state: StateDB, dry_run: bool) -> dict[str, FileRecord]:
    """The pair's last common state, unless it belongs to other folders or another
    account (changed in the UI or by hand, maybe while a run with the old ones was
    still going): then the run is a first run, which never deletes or overwrites."""
    stored = state.identity(pair.name)
    if stored is not None and stored != pair.identity:
        log.info(
            "pair %s: its folders or account changed since its state was recorded; "
            "this is a first run again (no deletes, no overwrites)", pair.name,
        )  # fmt: skip
        if dry_run:
            return {}
        state.reset(pair.name)
    if not dry_run and stored != pair.identity:
        state.set_identity(pair.name, pair.identity)  # also for state from before identities
    return state.records(pair.name)


def _check_present(provider: Provider, files: dict[str, FileMeta], what: str, known: int) -> None:
    """Stop when a side lists nothing because its folder is gone (see ``FolderMissing``).

    Providers that can tell a missing root from an empty one implement
    ``root_exists()``; the others are trusted.
    """
    root_exists = getattr(provider, "root_exists", None)
    if not files and root_exists is not None and not root_exists():
        raise FolderMissing(
            f"the {what} is missing, but {known} files were synced before; "
            "nothing was changed. Check that the folder (or its shared folder) exists."
        )
