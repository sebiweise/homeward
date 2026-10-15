"""One sync run for one pair: list both sides, plan, and (unless dry run) execute."""

import time
from dataclasses import dataclass
from datetime import date

from homeward.core.config import PairConfig
from homeward.core.executor import Executor, RunResult
from homeward.core.models import FileMeta, FolderMissing, Plan
from homeward.core.planner import build_plan
from homeward.core.progress import Progress
from homeward.core.provider import Provider
from homeward.core.state import StateDB


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
    records = state.records(pair.name)
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
        executor = Executor(local, remote, state, pair.name, progress)
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
