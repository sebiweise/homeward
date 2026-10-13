"""Executes a sync plan against providers."""

import logging
from dataclasses import dataclass, field, replace

from homeward.core.models import Action, ActionKind, FileMeta, Plan
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

    def __init__(self, local: Provider, remote: Provider, state: StateDB, pair: str) -> None:
        self.local = local
        self.remote = remote
        self.state = state
        self.pair = pair

    def run(self, plan: Plan, *, allow_deletes: bool = False) -> RunResult:
        if plan.blocked and not allow_deletes:
            return RunResult(blocked=plan.blocked)
        result = RunResult()
        for action in plan.actions:
            try:
                self._apply(action)
            except Exception as exc:
                failed = {"event": "file_failed"}
                log.warning("%s %s failed: %s", action.kind, action.path, exc, extra=failed)
                result.failed.append(Failure(action, f"{type(exc).__name__}: {exc}"))
            else:
                result.done.append(action)
        return result

    def _apply(self, action: Action) -> None:
        path = action.path
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

    @staticmethod
    def _transfer(src: Provider, dst: Provider, meta: FileMeta, overwrite: bool) -> FileMeta:
        if overwrite:
            dst.delete(meta.path)
        with src.open_read(meta.path) as stream:
            return dst.write(meta.path, stream, meta.mtime)


def _need[T](value: T | None) -> T:
    if value is None:
        raise ValueError("plan action is missing required data")
    return value
