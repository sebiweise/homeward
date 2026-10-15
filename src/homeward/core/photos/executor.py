"""Apply a photos plan: downloads in parallel, links, and moves to the trash."""

import logging
import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path

from homeward.core.files import link_noreplace, rename_noreplace, temp_name
from homeward.core.models import FolderMissing
from homeward.core.photo_state import PhotoStateDB
from homeward.core.photos.model import (
    CHUNK,
    FILE_KINDS,
    MAX_WORKERS,
    PREPARE_BATCH,
    PROGRESS_EVERY,
    PhotoAction,
    PhotoActionKind,
    PhotoFailure,
    PhotoItem,
    PhotoLayout,
    PhotoPlan,
    PhotoRunResult,
    PhotoSource,
)
from homeward.core.photos.plan import (
    build_photo_plan,
)
from homeward.core.planner import DeleteBrake
from homeward.core.progress import Progress
from homeward.core.trash import Trash

# One logger for the whole photos package, as before the split.
log = logging.getLogger("homeward.core.photos")


class PhotoExecutor:
    """Applies a photo plan; every finished step is recorded, failed steps are retried
    by the next run."""

    def __init__(
        self,
        source: PhotoSource,
        root: Path,
        state: PhotoStateDB,
        job: str,
        *,
        today: Callable[[], date] = date.today,
        workers: int = 1,
        progress: Progress | None = None,
        recycle_bin: Path | None = None,
    ) -> None:
        self.source = source
        self.trash = Trash(root, recycle_bin=recycle_bin, today=today)
        self.progress = progress or Progress()
        self.root = root
        self.state = state
        self.job = job
        self.workers = max(1, min(workers, MAX_WORKERS))
        self._bytes = 0
        self._fetched = 0
        self._files = 0
        self._started = 0.0

    def run(self, plan: PhotoPlan, *, allow_deletes: bool = False) -> PhotoRunResult:
        result = PhotoRunResult()
        # Files first, so links of the same run find their target.
        order = {kind: i for i, kind in enumerate(PhotoActionKind)}
        actions = sorted(plan.actions, key=lambda a: order[a.kind])
        files = [a for a in actions if a.kind in FILE_KINDS]
        self._bytes, self._fetched, self._files = 0, 0, len(files)
        self._started = time.monotonic()
        if files:
            sizes = [_need(a.item).size for a in files]
            known = None if None in sizes else sum(s or 0 for s in sizes)
            self.progress.phase("downloading", total=len(files), total_bytes=known or None)
        # Downloads run in parallel (each request waits mostly on Apple, not on bandwidth);
        # the state database and everything else stay on this thread.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            try:
                self._run_files(result, files, pool)
            except BaseException:
                # Cancelled: let the downloads already running finish (each one
                # writes a temporary file first), drop the queued ones.
                pool.shutdown(cancel_futures=True)
                raise
        rest = [a for a in actions if a.kind not in FILE_KINDS]
        if plan.blocked and not allow_deletes:
            log.warning(
                "delete brake: %s; photos deleted in iCloud stay on the NAS until approved",
                plan.blocked,
                extra={"event": "delete_brake"},
            )
            result.blocked = plan.blocked
            rest = [
                a for a in rest if a.kind is not PhotoActionKind.DELETE or a.key not in plan.held
            ]
            self.state.clear_full_listing(self.job)  # the approving run reads it all again
        if rest:
            self.progress.phase("linking", total=len(rest))
        for action in rest:
            self._step(result, action, partial(self._apply, action))
        # A run the delete brake held keeps neither: the next one reads everything again.
        if result.ok and not plan.unchanged and not result.blocked:
            self.state.set_cursor(self.job, plan.cursor)
            if plan.full:
                self.state.set_full_listing(self.job, plan.started_at, plan.selection)
        return result

    def _run_files(
        self, result: PhotoRunResult, files: list[PhotoAction], pool: ThreadPoolExecutor
    ) -> None:
        # Two batches are in flight: while the workers download one, the next one's
        # links are renewed and queued, so no worker waits for a batch to drain.
        in_flight: list[tuple[list[PhotoAction], dict[Future[int], PhotoAction]]] = []
        for start in range(0, len(files), PREPARE_BATCH):
            batch = files[start : start + PREPARE_BATCH]
            self._prepare([_need(a.item) for a in batch])
            fetches = {
                pool.submit(self._fetch, action): action
                for action in batch
                if action.kind is not PhotoActionKind.REPLACE
            }
            in_flight.append((batch, fetches))
            if len(in_flight) > 1:
                self._finish_batch(result, *in_flight.pop(0))
        for batch, fetches in in_flight:
            self._finish_batch(result, batch, fetches)

    def _finish_batch(
        self,
        result: PhotoRunResult,
        batch: list[PhotoAction],
        fetches: "dict[Future[int], PhotoAction]",
    ) -> None:
        for future in as_completed(fetches):
            action = fetches[future]
            self._step(result, action, partial(self._collect, action, future))
        for action in batch:
            if action.kind is PhotoActionKind.REPLACE:
                self._step(result, action, partial(self._apply, action))

    def _step(self, result: PhotoRunResult, action: PhotoAction, apply: Callable[[], None]) -> None:
        before = self._bytes
        try:
            apply()
        except Exception as exc:
            log.warning(
                "%s %s failed: %s", action.kind, action.path, exc, extra={"event": "file_failed"}
            )
            result.failed.append(PhotoFailure(action, f"{type(exc).__name__}: {exc}"))
            failed = True
        else:
            result.done.append(action)
            failed = False
        nbytes = self._bytes - before
        if failed and action.item is not None and action.item.size:
            nbytes = action.item.size  # keeps the remaining bytes and the estimate right
        self.progress.advance(nbytes=nbytes, failed=failed, current=action.path)
        if action.kind in FILE_KINDS:
            self._fetched += 1
            if self._fetched % PROGRESS_EVERY == 0 or self._fetched == self._files:
                elapsed = max(time.monotonic() - self._started, 1e-6)
                log.info(
                    "%d of %d files downloaded (%.2f GB, %.1f MB/s)",
                    self._fetched, self._files, self._bytes / 1e9, self._bytes / 1e6 / elapsed,
                )  # fmt: skip

    def _prepare(self, items: list[PhotoItem]) -> None:
        try:
            self.source.prepare(items)
        except Exception as exc:  # downloads still try their URLs as listed
            log.warning("could not prepare the next downloads: %s", exc)

    def _apply(self, action: PhotoAction) -> None:
        match action.kind:
            case PhotoActionKind.DOWNLOAD | PhotoActionKind.RESTORE:
                self._download(action)
            case PhotoActionKind.REPLACE:
                old = (self.root / action.path).stat()
                self._trash(action.path)
                self._download(action)
                self._relink(action.key, action.path, old)
            case PhotoActionKind.MOVE:
                self._move(action)
            case PhotoActionKind.LINK:
                target = self.root / _need(action.target)
                self._link(target, self.root / action.path)
                self.state.add_link(self.job, action.path, action.key, action.album)
            case PhotoActionKind.UNLINK:
                self._unlink(action)
                self.state.remove_link(self.job, action.path)
            case PhotoActionKind.GONE:
                self.state.mark_gone(self.job, action.key)
            case PhotoActionKind.DELETE:
                self._delete(action)
            case PhotoActionKind.RETURNED:
                item = _need(action.item)
                size = (self.root / action.path).stat().st_size
                self.state.record(self.job, item.key, action.path, size, item.checksum)

    def _download(self, action: PhotoAction) -> None:
        self._record(action, self._fetch(action))

    def _fetch(self, action: PhotoAction) -> int:
        """Download into place; safe to run on a worker thread (no state access)."""
        item = _need(action.item)
        target = self.root / action.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_name(target)
        try:
            with self.source.open_read(item) as stream, tmp.open("xb") as out:
                shutil.copyfileobj(stream, out, CHUNK)
                out.flush()
                os.fsync(out.fileno())
            size = tmp.stat().st_size
            if item.size is not None and size != item.size:
                raise OSError(f"incomplete download: {size} of {item.size} bytes")
            stamp = item.created.timestamp()
            os.utime(tmp, (stamp, stamp))
            rename_noreplace(tmp, target)  # never replaces an existing file
        finally:
            tmp.unlink(missing_ok=True)
        return size

    def _collect(self, action: PhotoAction, future: "Future[int]") -> None:
        self._record(action, future.result())

    def _record(self, action: PhotoAction, size: int) -> None:
        item = _need(action.item)
        self._bytes += size
        self.state.record(self.job, item.key, action.path, size, item.checksum)

    def _trash(self, path: str) -> None:
        """Move a file to the share's Synology recycle bin (same layout DSM uses, so
        DSM's emptying schedule applies), else to ``.homeward-papierkorb/<date>/``."""
        self.trash.move(self.root / path, path)

    def _link(self, target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link_noreplace(target, link)
        except OSError as exc:
            if link.exists():
                raise
            # File systems without hard links get a copy instead.
            log.info("hard link %s failed (%s), copying instead", link, exc)
            tmp = temp_name(link)
            try:
                shutil.copy2(target, tmp)
                rename_noreplace(tmp, link)
            finally:
                tmp.unlink(missing_ok=True)

    def _unlink(self, action: PhotoAction) -> None:
        link = self.root / action.path
        if not link.exists():
            return
        target = self.root / action.target if action.target else None
        if target is not None and target.exists() and os.path.samefile(link, target):
            link.unlink()  # the Year/Month file still holds the data
        else:
            self._trash(action.path)  # a copy or an edited file: keep it recoverable

    def _delete(self, action: PhotoAction) -> None:
        """Move a photo deleted in iCloud and its album links to the trash."""
        target = self.root / action.path
        for path, (key, _album) in self.state.links(self.job).items():
            if key != action.key:
                continue
            link = self.root / path
            if link.exists() and target.exists() and os.path.samefile(link, target):
                link.unlink()  # the same file, kept once in the trash below
            elif link.exists():
                self._trash(path)
            self.state.remove_link(self.job, path)
        if target.exists():
            self._trash(action.path)
        self.state.forget(self.job, action.key)

    def _move(self, action: PhotoAction) -> None:
        """Follow a photo into its new library: same file, new place and key. Its
        album links are hard links to the same file and only change their key."""
        item, old_path = _need(action.item), _need(action.target)
        if action.path != old_path:
            target = self.root / action.path
            target.parent.mkdir(parents=True, exist_ok=True)
            rename_noreplace(self.root / old_path, target)
        size = (self.root / action.path).stat().st_size
        self.state.record(self.job, item.key, action.path, size, item.checksum)
        for path, (key, album) in self.state.links(self.job).items():
            if key == action.moved_from:
                self.state.add_link(self.job, path, item.key, album)
        self.state.forget(self.job, action.moved_from)

    def _relink(self, key: str, target_path: str, old: os.stat_result) -> None:
        """After a replace, point the album links of ``key`` at the new file."""
        target = self.root / target_path
        for path, (link_key, _album) in self.state.links(self.job).items():
            if link_key != key:
                continue
            link = self.root / path
            if link.exists():
                st = link.stat()
                if (st.st_dev, st.st_ino) == (old.st_dev, old.st_ino):
                    link.unlink()  # the old version is already in the trash
                else:
                    self._trash(path)
            self._link(target, link)


def _need[T](value: T | None) -> T:
    if value is None:
        raise ValueError("photo action is missing required data")
    return value


@dataclass(frozen=True, slots=True)
class PhotoOutcome:
    plan: PhotoPlan
    result: PhotoRunResult | None  # None for a dry run


def backup_photos(
    job: str,
    source: PhotoSource,
    root: Path,
    state: PhotoStateDB,
    layout: PhotoLayout | None = None,
    *,
    albums: bool = True,
    dry_run: bool = False,
    workers: int = 1,
    today: Callable[[], date] = date.today,
    progress: Progress | None = None,
    now: Callable[[], float] = time.time,
    mirror_deletes: bool = False,
    selection: str | None = None,
    brake: DeleteBrake | None = None,
    allow_deletes: bool = False,
    remove_gone: bool = False,
    recycle_bin: Path | None = None,
) -> PhotoOutcome:
    if not root.is_dir() and state.records(job):
        raise FolderMissing(
            f"the backup folder {root} is missing, but earlier runs backed up photos "
            "there; nothing was changed. Check that the folder (or its shared folder) exists."
        )
    plan = build_photo_plan(
        job, source, root, state, layout, albums=albums, progress=progress, now=now,
        mirror_deletes=mirror_deletes, selection=selection, brake=brake,
        remove_gone=remove_gone,
    )  # fmt: skip
    if dry_run:
        return PhotoOutcome(plan, None)
    executor = PhotoExecutor(
        source, root, state, job, today=today, workers=workers, progress=progress,
        recycle_bin=recycle_bin,
    )  # fmt: skip
    result = executor.run(plan, allow_deletes=allow_deletes)
    return PhotoOutcome(plan, result)
