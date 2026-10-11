"""Builds a sync plan by comparing NAS, iCloud and last-synced state."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import PurePosixPath

from homeward.core.models import (
    DELETE_KINDS,
    Action,
    ActionKind,
    Change,
    FileMeta,
    FileRecord,
    Plan,
    SyncMode,
)
from homeward.core.rules import Decision, decide, is_excluded


@dataclass(frozen=True, slots=True)
class DeleteBrake:
    """A run that would delete more than either limit is blocked until approved."""

    max_ratio: float = 0.10
    max_count: int = 50

    def check(self, deletes: int, tracked: int) -> str | None:
        if deletes > self.max_count:
            return f"{deletes} deletions exceed the limit of {self.max_count} files"
        if tracked and deletes / tracked > self.max_ratio:
            return f"{deletes} of {tracked} files would be deleted, more than {self.max_ratio:.0%}"
        return None


def conflict_name(path: str, day: date, taken: Iterable[str] = ()) -> str:
    """Return ``Name (Konflikt NAS 2026-10-11).ext``, numbered if that is taken."""
    taken = set(taken)
    p = PurePosixPath(path)
    base = f"{p.stem} (Konflikt NAS {day.isoformat()}"
    candidate = str(p.with_name(f"{base}){p.suffix}"))
    number = 2
    while candidate in taken:
        candidate = str(p.with_name(f"{base} {number}){p.suffix}"))
        number += 1
    return candidate


def _change(meta: FileMeta | None, record_version: str | None) -> Change:
    if record_version is None:
        return Change.ABSENT if meta is None else Change.NEW
    if meta is None:
        return Change.DELETED
    return Change.UNCHANGED if meta.version == record_version else Change.CHANGED


def _same_content(local: FileMeta, remote: FileMeta) -> bool | None:
    """True or False when the metadata settles it, None when contents must be read."""
    if local.size != remote.size:
        return False
    if local.sha256 is not None and remote.sha256 is not None:
        return local.sha256 == remote.sha256
    return None


class _Builder:
    def __init__(
        self,
        local_files: Mapping[str, FileMeta],
        remote_files: Mapping[str, FileMeta],
        today: date,
    ) -> None:
        self.local_files = local_files
        self.remote_files = remote_files
        self.today = today
        self.reserved: set[str] = set()

    def action(self, decision: Decision, path: str, row: tuple[Change, Change]) -> Action | None:
        local = self.local_files.get(path)
        remote = self.remote_files.get(path)
        reason = f"NAS {row[0]}, iCloud {row[1]}"
        match decision:
            case Decision.NOTHING | Decision.IGNORE:
                return None
            case Decision.UPLOAD:
                return Action(
                    ActionKind.UPLOAD, path, reason, local, remote, overwrite=remote is not None
                )
            case Decision.DOWNLOAD:
                return Action(
                    ActionKind.DOWNLOAD, path, reason, local, remote, overwrite=local is not None
                )
            case Decision.DELETE_REMOTE:
                return Action(ActionKind.DELETE_REMOTE, path, reason, local, remote)
            case Decision.DELETE_LOCAL:
                return Action(ActionKind.DELETE_LOCAL, path, reason, local, remote)
            case Decision.FORGET:
                return Action(ActionKind.FORGET, path, reason)
            case Decision.SAME_OR_CONFLICT | Decision.SAME_OR_UPLOAD | Decision.SAME_OR_DOWNLOAD:
                assert local is not None and remote is not None
                return self._same_or(decision, path, reason, local, remote)
        raise AssertionError(decision)

    def _same_or(
        self, decision: Decision, path: str, reason: str, local: FileMeta, remote: FileMeta
    ) -> Action:
        same = _same_content(local, remote)
        if same:
            return Action(ActionKind.RECORD, path, f"{reason}, same content", local, remote)
        verify = same is None
        if decision is Decision.SAME_OR_UPLOAD:
            return Action(
                ActionKind.UPLOAD, path, reason, local, remote, overwrite=True,
                verify_content=verify,
            )  # fmt: skip
        if decision is Decision.SAME_OR_DOWNLOAD:
            return Action(
                ActionKind.DOWNLOAD, path, reason, local, remote, overwrite=True,
                verify_content=verify,
            )  # fmt: skip
        taken = self.local_files.keys() | self.remote_files.keys() | self.reserved
        target = conflict_name(path, self.today, taken)
        self.reserved.add(target)
        return Action(
            ActionKind.CONFLICT, path, f"{reason}, contents differ", local, remote,
            verify_content=verify, conflict_path=target,
        )  # fmt: skip


def _first_run_guard(action: Action) -> Action:
    """The first run of a pair only copies what is missing: no deletes, no overwrites."""
    if action.kind in DELETE_KINDS:
        return replace(action, kind=ActionKind.SKIP, reason="first run never deletes")
    if action.overwrite:
        # With ``verify_content`` the executor still records the file if contents match.
        return replace(
            action, kind=ActionKind.SKIP, overwrite=False, reason="first run never overwrites"
        )
    return action


def build_plan(
    pair: str,
    mode: SyncMode,
    local_files: Mapping[str, FileMeta],
    remote_files: Mapping[str, FileMeta],
    records: Mapping[str, FileRecord],
    *,
    excludes: Iterable[str] = (),
    brake: DeleteBrake | None = None,
    today: date | None = None,
) -> Plan:
    """Compare both sides with the last common state and decide every path."""
    patterns = tuple(excludes)
    local = {p: m for p, m in local_files.items() if not is_excluded(p, patterns)}
    remote = {p: m for p, m in remote_files.items() if not is_excluded(p, patterns)}
    known = {p: r for p, r in records.items() if not is_excluded(p, patterns)}

    first_run = not known
    plan = Plan(pair=pair, mode=mode, first_run=first_run, tracked=len(known))
    builder = _Builder(local, remote, today or date.today())
    for path in sorted(local.keys() | remote.keys() | known.keys()):
        record = known.get(path)
        row = (
            _change(local.get(path), record.local_version if record else None),
            _change(remote.get(path), record.remote_version if record else None),
        )
        action = builder.action(decide(mode, *row), path, row)
        if action is None:
            continue
        plan.actions.append(_first_run_guard(action) if first_run else action)

    plan.blocked = (brake or DeleteBrake()).check(plan.deletes, plan.tracked)
    return plan
