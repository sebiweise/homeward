"""Plan one run of a photos job: what to download, link, unlink or delete."""

import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from homeward.core.models import RuleHit
from homeward.core.photo_state import PhotoRecord, PhotoStateDB
from homeward.core.photos.model import (
    FILE_KINDS,
    FULL_LISTING_HOURS,
    Library,
    PhotoAction,
    PhotoActionKind,
    PhotoItem,
    PhotoLayout,
    PhotoPlan,
    PhotoSource,
    safe_name,
)
from homeward.core.planner import DeleteBrake
from homeward.core.progress import Progress
from homeward.core.trash import numbered
from homeward.core.workflow import DEFAULT_ARCHIVE_LIMIT, FileRule, RuleAction, first_match

# One logger for the whole photos package, as before the split.
log = logging.getLogger("homeward.core.photos")


class _Paths:
    """Hands out NAS paths that no other photo or link owns and that are free on disk.

    Names are compared ignoring case: Synology Drive (and SMB clients on macOS or
    Windows) see ``IMG_1.JPG`` and ``img_1.jpg`` as the same file and rename one of them.
    """

    def __init__(self, root: Path, owned: Iterable[str]) -> None:
        self.root = root
        self.taken = {p.casefold() for p in owned}
        self._listings: dict[PurePosixPath, set[str]] = {}

    def claim(self, wanted: PurePosixPath) -> str:
        candidate, number = wanted, 2
        while str(candidate).casefold() in self.taken or self._on_disk(candidate):
            candidate = numbered(wanted, number)
            number += 1
        self.taken.add(str(candidate).casefold())
        return str(candidate)

    def _on_disk(self, path: PurePosixPath) -> bool:
        return path.name.casefold() in _listing(self.root, path.parent, self._listings)


class _Folders:
    """Gives every album its own folder; folders differing only in case count as one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.used: dict[str, PurePosixPath] = {}  # casefolded folder or parent -> folder
        self.leaves: set[str] = set()  # casefolded album folders
        self._listings: dict[PurePosixPath, set[str]] = {}

    def keep(self, folder: PurePosixPath) -> PurePosixPath:
        self._register(folder)
        return folder

    def claim(self, wanted: PurePosixPath) -> PurePosixPath:
        parent = self._canonical(wanted.parent)
        name, number = wanted.name, 2
        while self._taken(parent / name):
            name = f"{wanted.name} ({number})"
            number += 1
        return self.keep(parent / name)

    def _canonical(self, folder: PurePosixPath) -> PurePosixPath:
        """``folder`` with each part spelled like a folder already in use."""
        result = PurePosixPath()
        for part in folder.parts:
            result = self.used.get(str(result / part).casefold(), result / part)
        return result

    def _taken(self, folder: PurePosixPath) -> bool:
        key = str(folder).casefold()
        if key in self.leaves or (key in self.used and self.used[key] != folder):
            return True
        names = _listing(self.root, folder.parent, self._listings)
        return folder.name.casefold() in names and not (self.root / folder).is_dir()

    def _register(self, folder: PurePosixPath) -> None:
        self.leaves.add(str(folder).casefold())
        for prefix in [folder, *folder.parents]:
            if prefix.parts:
                self.used.setdefault(str(prefix).casefold(), prefix)


def _listing(root: Path, folder: PurePosixPath, cache: dict[PurePosixPath, set[str]]) -> set[str]:
    """Casefolded names in ``root / folder``, read once per plan."""
    if folder not in cache:
        try:
            cache[folder] = {entry.casefold() for entry in os.listdir(root / folder)}
        except OSError:
            cache[folder] = set()
    return cache[folder]


def _all_present(root: Path, records: Iterable[PhotoRecord], links: Iterable[str]) -> bool:
    """Every file iCloud still has is on the NAS (archived ones cannot be fetched again)."""
    live = (r for r in records if r.gone_at is None and r.archived_at is None)
    return all((root / r.path).exists() for r in live) and all(
        (root / path).exists() for path in links
    )


@dataclass(frozen=True, slots=True)
class PhotoRules:
    """A photos job's file rules and whether its source can delete photos in iCloud."""

    rules: Sequence[FileRule] = ()
    archive_limit: int = DEFAULT_ARCHIVE_LIMIT
    can_delete: bool = False

    def decide(self, item: PhotoItem, layout: PhotoLayout, now: float) -> FileRule | None:
        """The rule for one file: matched against its backup path, size and the date
        the photo was taken."""
        if not self.rules:
            return None
        path = str(layout.folder(item) / safe_name(item.filename, "photo"))
        return first_match(self.rules, path, item.size, item.created.timestamp(), now)


def build_photo_plan(
    job: str,
    source: PhotoSource,
    root: Path,
    state: PhotoStateDB,
    layout: PhotoLayout | None = None,
    *,
    albums: bool = True,
    progress: Progress | None = None,
    now: Callable[[], float] = time.time,
    mirror_deletes: bool = False,
    selection: str | None = None,
    brake: DeleteBrake | None = None,
    remove_gone: bool = False,
    rules: PhotoRules | None = None,
) -> PhotoPlan:
    """Compare iCloud with the state of the last run and decide what to fetch or link.

    With ``mirror_deletes``, photos deleted in iCloud are removed on the NAS too (into
    the trash), but only when this run reads the whole library with the same
    ``selection`` (libraries and file types, any text) as the last complete run: a
    photo that merely left the selection is not deleted, it only counts as gone.
    More deletions than ``brake`` allows are held back until approved.

    ``remove_gone`` (the user's explicit "clean up") also deletes the photos earlier
    runs marked as deleted in iCloud, the same way and without the brake.

    When only some photos changed since the last run, the source is asked for just
    those (``list_changes``) instead of the whole library. The whole library is still
    read when the source cannot tell for sure, when a file is missing on the NAS, and
    at least every ``FULL_LISTING_HOURS``; that run also catches up on photos removed
    in iCloud and on changed album memberships.

    ``rules`` are the job's file rules: ignored files are neither downloaded nor
    marked as gone; archive rules (whole listings only) delete a backed-up photo in
    iCloud once all its files are verified on the NAS. Archived photos stay on the NAS
    for good, whatever ``mirror_deletes`` says.
    """
    layout = layout or PhotoLayout()
    rules = rules or PhotoRules()
    progress = progress or Progress()
    progress.phase("checking")
    records = state.records(job)
    links = state.links(job)
    cursor = source.cursor()
    previous = state.cursor(job)
    plan = PhotoPlan(job=job, cursor=cursor, started_at=now(), selection=selection)
    present = _all_present(root, records.values(), links)
    if remove_gone:
        plan.actions += [
            PhotoAction(PhotoActionKind.DELETE, r.path, key=key)
            for key, r in sorted(records.items())
            if r.gone_at is not None
        ]
    if (
        cursor is not None
        and cursor == previous
        and present
        and not _archive_due(job, state, plan, rules)
    ):
        plan.unchanged = True
        plan.up_to_date = sum(1 for r in records.values() if r.gone_at is None)
        return plan

    paths = _Paths(root, [r.path for r in records.values()] + list(links))
    changes = _changes(job, source, state, cursor, previous, present, plan.started_at)
    if changes is not None:
        plan.full = False
        progress.phase("planning", total=len(changes))
        changed = {i.key for i in changes}
        plan.actions = [a for a in plan.actions if a.key not in changed]  # back in iCloud
        moves = _Moves(root, records, changed)
        _, wanted_links = _plan_items(
            plan, changes, root, records, paths, layout, progress, albums, moves,
            _Ignore(rules, layout, plan.started_at),
        )  # fmt: skip
        plan.actions = [a for a in plan.actions if a.key not in moves.old_keys]
        links = moves.rekey(links)
        fetched = {a.key for a in plan.actions if a.kind in FILE_KINDS}
        plan.up_to_date = sum(
            1 for k, r in records.items() if r.gone_at is None and k not in fetched
        )
        existing = {(key, album): path for path, (key, album) in links.items()}
        _plan_links(plan, root, layout, paths, existing, wanted_links)
        return plan

    progress.phase("listing")  # the source sets the total once it knows it
    items = sorted(source.list_items(), key=lambda i: (i.created, i.key))
    progress.phase("planning", total=len(items))
    listed = {i.key for i in items}
    moves = _Moves(root, records, listed)
    seen, wanted_links = _plan_items(
        plan, items, root, records, paths, layout, progress, albums, moves,
        _Ignore(rules, layout, plan.started_at),
    )  # fmt: skip
    seen |= moves.old_keys  # moved, not deleted
    seen |= {k for k, r in records.items() if r.archived_at is not None}
    links = moves.rekey(links)
    delete = mirror_deletes and _same_selection(job, state, selection)
    live = sum(1 for r in records.values() if r.gone_at is None)
    for key, record in sorted(records.items()):
        if key not in seen and record.gone_at is None:
            kind = PhotoActionKind.DELETE if delete else PhotoActionKind.GONE
            plan.actions.append(PhotoAction(kind, record.path, key=key))
    # A photo marked as gone that is back in iCloud is not cleaned up.
    plan.actions = [
        a for a in plan.actions if a.kind is not PhotoActionKind.DELETE or a.key not in seen
    ]
    mirrored = (
        {k for k, r in records.items() if k not in seen and r.gone_at is None} if delete else set()
    )
    if mirrored:
        plan.blocked = (brake or DeleteBrake()).check(len(mirrored), live)
        if plan.blocked:
            plan.held = mirrored
    existing = {(key, album): path for path, (key, album) in links.items()}
    _plan_links(plan, root, layout, paths, existing, wanted_links)
    _plan_unlinks(plan, records, existing, wanted_links, seen)
    if rules.rules:
        _plan_archives(plan, items, root, records, layout, rules)
    return plan


def _archive_due(job: str, state: PhotoStateDB, plan: PhotoPlan, rules: PhotoRules) -> bool:
    """Archive rules depend on time, not only on changes in iCloud: with one, the
    library is read in full at least every ``FULL_LISTING_HOURS`` even if unchanged."""
    if not any(r.enabled and r.action is RuleAction.ARCHIVE for r in rules.rules):
        return False
    full_at = state.full_listing_at(job)
    return full_at is None or plan.started_at - full_at >= FULL_LISTING_HOURS * 3600


class _Ignore:
    """Files an ignore rule matches: recorded as hits, never downloaded."""

    def __init__(self, rules: PhotoRules, layout: PhotoLayout, now: float) -> None:
        self.rules, self.layout, self.now = rules, layout, now

    def __call__(self, plan: PhotoPlan, item: PhotoItem) -> bool:
        rule = self.rules.decide(item, self.layout, self.now)
        if rule is None or rule.action is not RuleAction.IGNORE:
            return False
        plan.rule_hits.append(RuleHit(item.filename, rule.name, RuleAction.IGNORE))
        return True


def _plan_archives(
    plan: PhotoPlan,
    items: list[PhotoItem],
    root: Path,
    records: dict[str, PhotoRecord],
    layout: PhotoLayout,
    rules: PhotoRules,
) -> None:
    """Archive photos whose original an archive rule matches, all files backed up.

    A photo goes as a whole (original, Live Photo video, RAW, edits): iCloud deletes
    them together. Only the personal library: deleting in the Shared Library would
    take the photo from everyone who shares it.
    """
    busy = {a.key for a in plan.actions}
    by_asset: dict[tuple[str, str], list[PhotoItem]] = {}
    for item in items:
        by_asset.setdefault((item.library, item.asset_id), []).append(item)
    for files in by_asset.values():
        original = next((f for f in files if f.resource == "original"), None)
        if original is None:
            continue
        rule = rules.decide(original, layout, plan.started_at)
        if rule is None or rule.action is not RuleAction.ARCHIVE:
            continue
        pending = _archive_pending(original, files, root, records, busy, rule, rules)
        record = records.get(original.key)
        shown = record.path if record is not None else original.filename
        plan.rule_hits.append(RuleHit(shown, rule.name, rule.action, pending))
        if pending is None:
            plan.actions.append(
                PhotoAction(PhotoActionKind.ARCHIVE, records[original.key].path, original,
                            original.key, archive_items=tuple(files), rule=rule.name)
            )  # fmt: skip
    if plan.archives > rules.archive_limit:
        plan.archive_blocked = (
            f"{plan.archives} photos would be archived, more than the limit of "
            f"{rules.archive_limit}"
        )


def _archive_pending(
    original: PhotoItem,
    files: list[PhotoItem],
    root: Path,
    records: dict[str, PhotoRecord],
    busy: set[str],
    rule: FileRule,
    rules: PhotoRules,
) -> str | None:
    """Why the photo is not archived in this run, or None when it can be."""
    if rule.preview:
        return "preview"
    if original.library is not Library.PERSONAL:
        return "shared library"
    if not rules.can_delete:
        return "not supported"
    if not records:
        return "first run"
    for item in files:
        record = records.get(item.key)
        if (
            record is None
            or item.key in busy
            or record.gone_at is not None
            or not (root / record.path).is_file()
            or _changed(record, item)
        ):
            return "needs another sync"
    return None


def _same_selection(job: str, state: PhotoStateDB, selection: str | None) -> bool:
    previous = state.full_listing_selection(job)
    if previous is None or previous != selection:
        log.info(
            "photos removed from iCloud are not deleted on the NAS in this run: "
            "the libraries or file types changed since the last complete run"
        )
        return False
    return True


def _changes(
    job: str,
    source: PhotoSource,
    state: PhotoStateDB,
    cursor: str | None,
    previous: str | None,
    present: bool,
    now: float,
) -> list[PhotoItem] | None:
    """The changed photos when reading only the changes is safe, else None."""
    full_at = state.full_listing_at(job)
    if cursor is None or previous is None or full_at is None:
        reason = "no earlier complete run to build on"
    elif not present:
        reason = "files are missing on the NAS"
    elif now - full_at >= FULL_LISTING_HOURS * 3600:
        reason = f"the last complete reading is more than {FULL_LISTING_HOURS} hours old"
    else:
        changed = source.list_changes(previous)
        if changed is not None:
            items = sorted(changed, key=lambda i: (i.created, i.key))
            log.info("reading only what changed in iCloud: %d files", len(items))
            return items
        reason = "iCloud did not report the changes in a way Homeward can rely on"
    log.info("reading the whole photo library: %s", reason)
    return None


def _plan_items(
    plan: PhotoPlan,
    items: list[PhotoItem],
    root: Path,
    records: dict[str, PhotoRecord],
    paths: _Paths,
    layout: PhotoLayout,
    progress: Progress,
    albums: bool,
    moves: "_Moves",
    ignore: "_Ignore",
) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Download what is new, restore or replace what changed, move what changed library.
    Returns the keys seen in iCloud and the album links wanted, as (key, album) ->
    primary path."""
    wanted_links: dict[tuple[str, str], str] = {}
    # key -> NAS path, for placing sidecars (next to a photo of an earlier run, too)
    assigned = {key: record.path for key, record in records.items()}
    seen: set[str] = set()
    for item in items:
        progress.advance()
        if item.key in seen:
            continue
        seen.add(item.key)
        record = records.get(item.key)
        if ignore(plan, item):
            if record is not None:  # backed up before the rule: keep it and its links
                assigned[item.key] = record.path
                if albums:
                    wanted_links.update(((item.key, a), record.path) for a in item.albums)
            continue
        old = moves.match(item) if record is None else None
        if old is not None:
            wanted = _new_path(item, layout, assigned)
            path = old.path if str(wanted) == old.path else paths.claim(wanted)
            plan.actions.append(
                PhotoAction(PhotoActionKind.MOVE, path, item, item.key, target=old.path,
                            moved_from=old.key)
            )  # fmt: skip
        elif record is None:
            path = paths.claim(_new_path(item, layout, assigned))
            plan.actions.append(PhotoAction(PhotoActionKind.DOWNLOAD, path, item, item.key))
        else:
            path = record.path
            kind = _record_action(root, record, item)
            if kind is None:
                plan.up_to_date += 1
            else:
                plan.actions.append(PhotoAction(kind, path, item, item.key))
        assigned[item.key] = path
        if albums:
            for album in item.albums:
                wanted_links[(item.key, album)] = path
    return seen, wanted_links


class _Moves:
    """Photos that moved to another library in iCloud, e.g. into the Shared Library.

    Their key changes with the library (``personal/...`` to ``shared/...``), so the
    move looks like a new photo plus a deleted one. A new file whose checksum and
    resource match a backed-up file of another library that this run did not list
    (any longer) is that file: it is moved on the NAS instead of downloaded again,
    and the old one is not counted as deleted.
    """

    def __init__(self, root: Path, records: dict[str, PhotoRecord], listed: set[str]) -> None:
        self._by_content: dict[tuple[str, str], PhotoRecord] = {}
        for key, record in sorted(records.items()):
            if (
                record.checksum
                and record.archived_at is None
                and key not in listed
                and (root / record.path).exists()
            ):
                self._by_content.setdefault((_resource(key), record.checksum), record)
        self.old_keys: set[str] = set()
        self._new_keys: dict[str, str] = {}  # old key -> new key

    def match(self, item: PhotoItem) -> PhotoRecord | None:
        if item.checksum is None:
            return None
        record = self._by_content.get((item.resource, item.checksum))
        if record is None or _library(record.key) == str(item.library):
            return None
        del self._by_content[(item.resource, item.checksum)]
        self.old_keys.add(record.key)
        self._new_keys[record.key] = item.key
        return record

    def rekey(self, links: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        """The album links of moved photos belong to their new key."""
        return {path: (self._new_keys.get(key, key), album) for path, (key, album) in links.items()}


def _resource(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _library(key: str) -> str:
    return key.split("/", 1)[0]


def _new_path(item: PhotoItem, layout: PhotoLayout, assigned: dict[str, str]) -> PurePosixPath:
    beside = _sidecar_owner(item, assigned)
    if beside is not None:
        return beside.with_name(f"{beside.name}{PurePosixPath(item.filename).suffix}")
    return layout.folder(item) / safe_name(item.filename, "photo")


def _record_action(root: Path, record: PhotoRecord, item: PhotoItem) -> PhotoActionKind | None:
    """What a photo downloaded before needs, or None when it is up to date."""
    if not (root / record.path).exists():
        return PhotoActionKind.RESTORE
    if _changed(record, item):
        return PhotoActionKind.REPLACE
    if record.gone_at is not None or record.archived_at is not None:
        return PhotoActionKind.RETURNED
    return None


def _plan_links(
    plan: PhotoPlan,
    root: Path,
    layout: PhotoLayout,
    paths: _Paths,
    existing: dict[tuple[str, str], str],
    wanted_links: dict[tuple[str, str], str],
) -> None:
    """Link every photo into the folders of its albums."""
    folders = _album_folders(root, layout, existing, {key[1] for key in wanted_links})
    for (key, album), target in sorted(wanted_links.items()):
        link_path = existing.get((key, album))
        folder = folders[album]
        if link_path is not None and PurePosixPath(link_path).parent != folder:
            # The album moved to another folder (one only differing in case got renamed
            # by Synology Drive): link again there and forget the old place.
            if (root / link_path).exists():
                continue
            plan.actions.append(
                PhotoAction(PhotoActionKind.UNLINK, link_path, key=key, album=album)
            )
            link_path = None
        elif link_path is not None and (root / link_path).exists():
            continue
        if link_path is None:
            link_path = paths.claim(folder / PurePosixPath(target).name)
        plan.actions.append(
            PhotoAction(PhotoActionKind.LINK, link_path, key=key, target=target, album=album)
        )


def _plan_unlinks(
    plan: PhotoPlan,
    records: dict[str, PhotoRecord],
    existing: dict[tuple[str, str], str],
    wanted_links: dict[tuple[str, str], str],
    seen: set[str],
) -> None:
    """Remove the links of photos that left an album but are still in iCloud."""
    for (key, album), link_path in sorted(existing.items()):
        owner = records.get(key)
        if owner is not None and owner.archived_at is not None:
            continue  # gone from iCloud on purpose: the album folders keep it
        if (key, album) not in wanted_links and key in seen:
            plan.actions.append(
                PhotoAction(
                    PhotoActionKind.UNLINK,
                    link_path,
                    key=key,
                    target=owner.path if owner else None,
                    album=album,
                )
            )


def _album_folders(
    root: Path,
    layout: PhotoLayout,
    existing: dict[tuple[str, str], str],
    albums: Iterable[str],
) -> dict[str, PurePosixPath]:
    """The folder of every album: where its links already are, else a new one."""
    folders = _Folders(root)
    present: dict[str, PurePosixPath] = {}
    for (_key, album), path in sorted(existing.items()):
        if album not in present and (root / path).exists():
            present[album] = PurePosixPath(path).parent
    result = {album: folders.keep(folder) for album, folder in sorted(present.items())}
    for album in sorted(set(albums) - set(result)):
        result[album] = folders.claim(layout.album_folder(album))
    return result


def _sidecar_owner(item: PhotoItem, assigned: dict[str, str]) -> PurePosixPath | None:
    """The NAS path of the file a sidecar belongs to, so ``IMG (2).HEIC`` gets
    ``IMG (2).HEIC.xmp``."""
    if item.sidecar_for is None:
        return None
    owner = replace(item, resource=item.sidecar_for).key
    return PurePosixPath(assigned[owner]) if owner in assigned else None


def _changed(record: PhotoRecord, item: PhotoItem) -> bool:
    if item.checksum is not None and record.checksum is not None:
        return item.checksum != record.checksum
    return item.size is not None and record.size is not None and item.size != record.size
