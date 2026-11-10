"""One sync run for one pair: list both sides, plan, and (unless dry run) execute."""

import json
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
        rules=pair.rules,
        # A dry run with changed folders keeps the old state: do not apply its archive.
        archived=state.archived(pair.name)
        if _same_nas_folder(state.identity(pair.name), pair)
        else (),
        archive_limit=pair.archive_limit,
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


def _same_nas_folder(stored: str | None, pair: PairConfig) -> bool:
    """Whether state recorded under the identity ``stored`` is for the pair's NAS folder.

    None (state from before identities) and "" (reset, by whoever changed the pair and
    already kept or dropped the archived paths) count as the same folder, and so does an
    identity that cannot be read: keeping archived paths never uploads or deletes.
    """
    if not stored:
        return True
    try:
        old = json.loads(stored)
    except ValueError:
        old = None
    if not isinstance(old, list) or not old:
        log.warning(
            "%s: the stored state does not say which NAS folder it was for; archived files "
            "stay remembered",
            pair.name,
        )
        return True
    return bool(old[0] == str(pair.local))


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
        # Archived paths belong to the NAS folder: with another one they would hide its
        # files of the same name from the sync; with the same one (only the iCloud side
        # changed) they stay, or a bidirectional pair would upload them again.
        state.reset(pair.name, archived=not _same_nas_folder(stored, pair))
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
