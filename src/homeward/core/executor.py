"""Executes a sync plan against providers."""

import hashlib
import logging
from dataclasses import dataclass, field, replace
from typing import BinaryIO

from homeward.core.limits import Throttle, throttled
from homeward.core.models import Action, ActionKind, FileMeta, Plan
from homeward.core.progress import Progress
from homeward.core.provider import Provider
from homeward.core.state import StateDB

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Failure:
    action: Action
    error: str


@dataclass(slots=True)
class RunResult:
    done: list[Action] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    blocked: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.blocked is None


class Executor:
    """Applies a plan step by step and records each finished step in the state DB.

    A failing step is logged and skipped; the state of that path stays untouched, so
    the next run plans it again from what both sides then hold.
    """

    def __init__(
        self,
        local: Provider,
        remote: Provider,
        state: StateDB,
        pair: str,
        progress: Progress | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self.progress = progress or Progress()
        self.throttle = throttle
        self.local = local
        self.remote = remote
        self.state = state
        self.pair = pair

    def run(self, plan: Plan, *, allow_deletes: bool = False) -> RunResult:
        if plan.blocked and not allow_deletes:
            return RunResult(blocked=plan.blocked)
        result = RunResult()
        failed_paths: set[str] = set()
        self.progress.phase("transferring", total=len(plan.actions))
        for action in plan.actions:
            self.progress.advance(0, current=action.path)
            if action.renamed_to in failed_paths:
                # The new name did not arrive; keep the old file until a later run.
                log.info("keeping %s: its renamed copy %s failed", action.path, action.renamed_to)
                self.progress.advance()
                continue
            try:
                self._apply(action)
            except Exception as exc:
                failed = {"event": "file_failed"}
                log.warning("%s %s failed: %s", action.kind, action.path, exc, extra=failed)
                result.failed.append(Failure(action, f"{type(exc).__name__}: {exc}"))
                failed_paths.add(action.path)
                self.progress.advance(failed=True)
            else:
                result.done.append(action)
                self.progress.advance()
        return result

    def _apply(self, action: Action) -> None:
        path = action.path
        if action.kind is ActionKind.ARCHIVE:
            self._archive(action)
            return
        if action.verify_content and self._record_if_same(action):
            return
        match action.kind:
            case ActionKind.UPLOAD:
                local = _need(action.local)
                remote = self._transfer(self.local, self.remote, local, action.overwrite)
                self.state.record(self.pair, local, remote)
            case ActionKind.DOWNLOAD:
                remote = _need(action.remote)
                local = self._transfer(self.remote, self.local, remote, action.overwrite)
                self.state.record(self.pair, local, remote)
            case ActionKind.DELETE_REMOTE:
                self.remote.delete(path)
                self.state.forget(self.pair, path)
            case ActionKind.DELETE_LOCAL:
                self.local.delete(path)
                self.state.forget(self.pair, path)
            case ActionKind.FORGET:
                self.state.forget(self.pair, path)
            case ActionKind.RECORD:
                self.state.record(self.pair, _need(action.local), _need(action.remote))
            case ActionKind.CONFLICT:
                self._conflict(action)
            case ActionKind.SKIP:
                # Skipped actions are only reported, never executed.
                pass

    def _archive(self, action: Action) -> None:
        """Make sure the NAS holds exactly iCloud's file, then move it to the iCloud trash.

        The NAS copy is checked by its SHA-256 against iCloud's content (hashed while
        downloading, or known from the last sync, or read from iCloud). Any mismatch
        stops here and iCloud keeps the file.
        """
        remote = _need(action.remote)
        local = action.local
        expected = remote.sha256
        download = local is None or action.overwrite
        if action.verify_content and local is not None:
            expected = self.remote.sha256(remote.path)
            if self.local.sha256(local.path) == expected:
                download = False
                self.state.record(self.pair, replace(local, sha256=expected), remote)
        if download:
            digest = hashlib.sha256()
            local = self._transfer(self.remote, self.local, remote, local is not None, digest)
            expected = digest.hexdigest()
            self.state.record(self.pair, replace(local, sha256=expected), remote)
        assert local is not None
        if expected is None:
            expected = self.remote.sha256(remote.path)
        actual = self.local.sha256(local.path)
        if local.size != remote.size or actual != expected:
            raise ArchiveMismatch(
                f"the NAS copy of {action.path} differs from iCloud; iCloud keeps the file"
            )
        self.remote.delete(remote.path)  # into the iCloud trash, recoverable there
        self.state.archive(self.pair, local, actual, action.rule or "")
        log.info(
            "archived %s: kept on the NAS, moved to the iCloud trash (rule %r)",
            action.path, action.rule,
        )  # fmt: skip

    def _record_if_same(self, action: Action) -> bool:
        local, remote = _need(action.local), _need(action.remote)
        digest = self.local.sha256(local.path)
        if digest != self.remote.sha256(remote.path):
            return False
        self.state.record(self.pair, replace(local, sha256=digest), replace(remote, sha256=digest))
        return True

    def _conflict(self, action: Action) -> None:
        """Keep both: the NAS version moves aside, the iCloud version takes its place."""
        conflict_path = _need(action.conflict_path)
        moved = self.local.move(action.path, conflict_path)
        remote = _need(action.remote)
        local = self._transfer(self.remote, self.local, remote, overwrite=False)
        self.state.record(self.pair, local, remote)
        uploaded = self._transfer(self.local, self.remote, moved, overwrite=False)
        self.state.record(self.pair, moved, uploaded)

    def _transfer(
        self,
        src: Provider,
        dst: Provider,
        meta: FileMeta,
        overwrite: bool,
        digest: "hashlib._Hash | None" = None,
    ) -> FileMeta:
        if overwrite:
            dst.delete(meta.path)  # into the trash, so it stays recoverable
            # If the write does not finish (an error, a cancelled run), the next run
            # must not read the now missing file as a delete to pass on: without a
            # record it copies the file again.
            self.state.forget(self.pair, meta.path)
        with src.open_read(meta.path) as stream:
            data = throttled(stream, self.throttle)
            if digest is not None:
                data = _Hashing(data, digest)  # type: ignore[assignment]
            return dst.write(meta.path, data, meta.mtime)


class ArchiveMismatch(RuntimeError):
    """The NAS copy of a file to archive does not match iCloud's content."""


class _Hashing:
    """Feeds every byte read from ``stream`` into ``digest``."""

    def __init__(self, stream: BinaryIO, digest: "hashlib._Hash") -> None:
        self._stream = stream
        self._digest = digest

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._digest.update(chunk)
        return chunk

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _need[T](value: T | None) -> T:
    if value is None:
        raise ValueError("plan action is missing required data")
    return value
