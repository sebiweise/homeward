"""Builds a sync plan by comparing NAS, iCloud and last-synced state."""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    RuleHit,
    SyncMode,
)
from homeward.core.rules import Decision, decide, is_excluded
from homeward.core.workflow import DEFAULT_ARCHIVE_LIMIT, FileRule, RuleAction, first_match


@dataclass(frozen=True, slots=True)
class DeleteBrake:
    """A run that would delete more than either limit is blocked until approved.

    The ratio only counts from more than ``min_count`` deletions on, so tidying up a
    few files in a small folder does not need an approval every time.
    """

    max_ratio: float = 0.10
    max_count: int = 50
    min_count: int = 5

    def check(self, deletes: int, tracked: int) -> str | None:
        if deletes > self.max_count:
            return f"{deletes} deletions exceed the limit of {self.max_count} files"
        if deletes > self.min_count and tracked and deletes / tracked > self.max_ratio:
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


Identity = Callable[[str], str]
"""Maps a side's version token to the part that survives a rename (a file id)."""


def _content_matches(
    meta: FileMeta, record: FileRecord, record_version: str, identity: Identity | None
) -> bool:
    if meta.size != record.size:
        return False
    if meta.version == record_version:
        return True
    if identity is not None and identity(meta.version) == identity(record_version):
        return True
    return meta.sha256 is not None and meta.sha256 == record.sha256


def _match_renames(
    actions: list[Action],
    records: Mapping[str, FileRecord],
    remote_identity: Identity | None = None,
) -> list[Action]:
    """Mark deletes whose content shows up as a new file on the same side.

    A rename or move on one side looks like a deleted file plus a new one. It is still
    carried out as exactly that (the old file goes to the other side's trash, the new
    one is transferred), but the delete is not a loss, so the delete brake skips it.
    The NAS keeps size and mtime on a rename, so its version token matches the record.
    iCloud changes the etag but keeps the file id, which ``remote_identity`` extracts.
    Otherwise a known checksum must match.
    """
    new_local = [a for a in actions if a.kind is ActionKind.UPLOAD and a.path not in records]
    new_remote = [a for a in actions if a.kind is ActionKind.DOWNLOAD and a.path not in records]
    used: set[str] = set()
    result = []
    for action in actions:
        record = records.get(action.path)
        if action.kind in DELETE_KINDS and record is not None:
            local_side = action.kind is ActionKind.DELETE_REMOTE
            target = _rename_target(
                record,
                new_local if local_side else new_remote,
                local_side=local_side,
                identity=None if local_side else remote_identity,
                used=used,
            )
            if target is not None:
                used.add(target)
                action = replace(
                    action, renamed_to=target, reason=f"{action.reason}, renamed to {target}"
                )
        result.append(action)
    return result


def _rename_target(
    record: FileRecord,
    candidates: list[Action],
    *,
    local_side: bool,
    identity: Identity | None,
    used: set[str],
) -> str | None:
    """The first unused new file on that side whose content matches ``record``."""
    version = record.local_version if local_side else record.remote_version
    for new in candidates:
        meta = new.local if local_side else new.remote
        if new.path in used or meta is None:
            continue
        if _content_matches(meta, record, version, identity):
            return new.path
    return None


def _prefixes(path: str) -> list[str]:
    parts = PurePosixPath(path).parts
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _guard_case(actions: list[Action], remote_files: Mapping[str, FileMeta]) -> list[Action]:
    """Skip uploads whose path differs only in upper/lower case from one in iCloud.

    iCloud Drive does not keep two names apart that only differ in case, so such an
    upload could land on the other file. Files the plan deletes in iCloud do not count,
    which lets a case-only rename (``a.txt`` to ``A.txt``) through.
    """
    deleted = {a.path for a in actions if a.kind is ActionKind.DELETE_REMOTE}
    seen: dict[str, str] = {}
    for path in remote_files:
        if path not in deleted:
            seen.update((prefix.casefold(), prefix) for prefix in _prefixes(path))
    return [_case_checked(action, seen) for action in actions]


def _case_checked(action: Action, seen: dict[str, str]) -> Action:
    """Skip a new upload that clashes by case with ``seen``, else add it to ``seen``."""
    if action.kind is not ActionKind.UPLOAD or action.remote is not None:
        return action
    prefixes = _prefixes(action.path)
    clash = next((seen[p.casefold()] for p in prefixes if seen.get(p.casefold(), p) != p), None)
    if clash is None:
        seen.update((p.casefold(), p) for p in prefixes)
        return action
    return replace(
        action,
        kind=ActionKind.SKIP,
        reason=f"iCloud already has {clash}, which differs only in upper/lower case",
    )


def _order(actions: list[Action]) -> list[Action]:
    """Run deletes after all transfers, so a rename trashes the old file only once the
    new one has arrived. A delete whose path equals a transferred one apart from case
    runs first, because on iCloud the two would collide."""
    created = {
        a.path.casefold() for a in actions if a.kind in (ActionKind.UPLOAD, ActionKind.DOWNLOAD)
    }

    def rank(action: Action) -> int:
        if action.kind not in DELETE_KINDS:
            return 1
        return 0 if action.path.casefold() in created else 2

    return sorted(actions, key=rank)


def _ignored(
    rules: Sequence[FileRule],
    path: str,
    metas: Iterable[FileMeta | None],
    record: FileRecord | None,
    now: float,
) -> FileRule | None:
    """The ignore rule that matches the file on either side (or its last synced size)."""
    candidates: list[tuple[int, float | None]] = [(m.size, m.mtime) for m in metas if m]
    if not candidates and record is not None:
        candidates.append((record.size, None))
    for size, mtime in candidates:
        rule = first_match(rules, path, size, mtime, now)
        if rule is not None and rule.action is RuleAction.IGNORE:
            return rule
    return None


def _apply_ignores(
    rules: Sequence[FileRule],
    local: dict[str, FileMeta],
    remote: dict[str, FileMeta],
    known: dict[str, FileRecord],
    now: float,
) -> list[RuleHit]:
    """Drop every file an ignore rule matches from all three views, like an exclude."""
    hits = []
    for path in sorted(local.keys() | remote.keys() | known.keys()):
        rule = _ignored(rules, path, (local.get(path), remote.get(path)), known.get(path), now)
        if rule is not None:
            local.pop(path, None)
            remote.pop(path, None)
            known.pop(path, None)
            hits.append(RuleHit(path, rule.name, RuleAction.IGNORE))
    return hits


def _archive_step(
    action: Action | None,
    path: str,
    local: FileMeta | None,
    remote: FileMeta,
    record: FileRecord | None,
) -> Action | None:
    """The archive action for a file whose normal step leaves the NAS with iCloud's
    content, or None when another step must happen first (it changed on the NAS, a
    conflict, an upload)."""
    if action is None:
        # Nothing to do: archive only what both sides still hold as last synced.
        if (
            record is None
            or local is None
            or local.version != record.local_version
            or remote.version != record.remote_version
        ):
            return None
        remote = replace(remote, sha256=remote.sha256 or record.sha256)
        return Action(ActionKind.ARCHIVE, path, "in sync", local, remote)
    if action.kind is ActionKind.RECORD:
        return replace(action, kind=ActionKind.ARCHIVE)
    if action.kind is ActionKind.DOWNLOAD:
        return replace(action, kind=ActionKind.ARCHIVE)
    return None


def _apply_archives(
    plan: Plan,
    rules: Sequence[FileRule],
    local: Mapping[str, FileMeta],
    remote: Mapping[str, FileMeta],
    known: Mapping[str, FileRecord],
    now: float,
) -> None:
    """Turn the steps of files an archive rule matches into archive actions."""
    by_path = {a.path: a for a in plan.actions}
    for path, meta in sorted(remote.items()):
        rule = first_match(rules, path, meta.size, meta.mtime, now)
        if rule is None or rule.action is not RuleAction.ARCHIVE:
            continue
        if rule.preview:
            plan.rule_hits.append(RuleHit(path, rule.name, rule.action, "preview"))
            continue
        if plan.first_run:
            plan.rule_hits.append(RuleHit(path, rule.name, rule.action, "first run"))
            continue
        current = by_path.get(path)
        step = _archive_step(current, path, local.get(path), meta, known.get(path))
        if step is None:
            plan.rule_hits.append(RuleHit(path, rule.name, rule.action, "needs another sync"))
            continue
        step = replace(step, rule=rule.name, reason=f"{step.reason}, rule {rule.name!r}")
        plan.rule_hits.append(RuleHit(path, rule.name, rule.action))
        if current is None:
            plan.actions.append(step)
        else:
            plan.actions[plan.actions.index(current)] = step


def _blocked(plan: Plan, brake: DeleteBrake, archive_limit: int) -> str | None:
    reason = brake.check(plan.deletes, plan.tracked)
    if reason is None and plan.archives > archive_limit:
        reason = f"{plan.archives} files would be archived, more than the limit of {archive_limit}"
    return reason


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
    remote_case_insensitive: bool = False,
    remote_identity: Identity | None = None,
    rules: Sequence[FileRule] = (),
    archived: Iterable[str] = (),
    archive_limit: int = DEFAULT_ARCHIVE_LIMIT,
    now: float | None = None,
) -> Plan:
    """Compare both sides with the last common state and decide every path.

    ``rules`` are the pair's file rules: ignored files drop out like excluded ones,
    archive rules turn the step of a matching iCloud file into an archive action.
    ``archived`` lists paths archived before: while iCloud does not have them again,
    their NAS copy is left alone (never uploaded, never deleted).
    """
    now = time.time() if now is None else now
    patterns = tuple(excludes)
    local = {p: m for p, m in local_files.items() if not is_excluded(p, patterns)}
    remote = {p: m for p, m in remote_files.items() if not is_excluded(p, patterns)}
    known = {p: r for p, r in records.items() if not is_excluded(p, patterns)}
    for path in archived:
        if path not in remote:
            local.pop(path, None)
    hits = _apply_ignores(rules, local, remote, known, now)

    first_run = not known
    plan = Plan(pair=pair, mode=mode, first_run=first_run, tracked=len(known), rule_hits=hits)
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

    if remote_case_insensitive:
        plan.actions = _guard_case(plan.actions, remote)
    plan.actions = _match_renames(plan.actions, known, remote_identity)
    if mode is not SyncMode.UPLOAD:
        _apply_archives(plan, rules, local, remote, known, now)
    plan.actions = _order(plan.actions)
    plan.blocked = _blocked(plan, brake or DeleteBrake(), archive_limit)
    return plan
