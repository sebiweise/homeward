"""The weekly check that the backed-up photos on the NAS are still whole.

A file whose size no longer matches what was downloaded (cut off, overwritten,
damaged) is moved to the trash, never deleted, and the run that follows downloads
it again, as it does for a file missing on the NAS. Its album links (hard links to
the same damaged data) are removed so that run links the new download. A photo
iCloud no longer has stays where it is: the NAS copy is all there is.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from homeward.core.photo_state import PhotoStateDB
from homeward.core.trash import Trash

log = logging.getLogger("homeward.core.photos")

INTEGRITY_DAYS = 7


@dataclass(slots=True)
class IntegrityResult:
    checked: int = 0
    damaged: list[str] = field(default_factory=list)  # moved to the trash, to download again


def integrity_due(state: PhotoStateDB, job: str, now: float) -> bool:
    last = state.integrity_checked_at(job)
    return last is None or now - last >= INTEGRITY_DAYS * 86400


def check_integrity(
    job: str, root: Path, state: PhotoStateDB, trash: Trash, now: float
) -> IntegrityResult:
    """Compare every backed-up photo with the size it was downloaded with."""
    result = IntegrityResult()
    links: dict[str, list[str]] = {}
    for link_path, (key, _album) in state.links(job).items():
        links.setdefault(key, []).append(link_path)
    for record in state.records(job).values():
        if record.size is None:
            continue
        path = root / record.path
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue  # missing files are downloaded again by the plan anyway
        except OSError as exc:
            log.warning("integrity check: %s cannot be read: %s", record.path, exc)
            continue
        result.checked += 1
        size = stat.st_size
        if size == record.size:
            continue
        if record.gone_at is not None:
            log.warning(
                "integrity check: %s has %d bytes instead of %d; iCloud no longer has it, "
                "so it stays as it is", record.path, size, record.size,
                extra={"event": "integrity_damaged"},
            )  # fmt: skip
            continue
        log.warning(
            "integrity check: %s has %d bytes instead of %d; moving it to the trash "
            "and downloading it again",
            record.path, size, record.size, extra={"event": "integrity_repaired"},
        )  # fmt: skip
        try:
            trash.move(path, record.path)
        except OSError as exc:
            log.warning("integrity check: could not move %s aside: %s", record.path, exc)
            continue
        result.damaged.append(record.path)
        for link_path in links.get(record.key, []):
            link = root / link_path
            try:
                if link.stat().st_ino != stat.st_ino:
                    continue  # not (any more) a link to the damaged file: leave it
                link.unlink()  # the data stays in the trash copy
            except FileNotFoundError:
                pass
            state.remove_link(job, link_path)
    state.set_integrity_checked(job, now)
    log.info("integrity check: %d photos checked, %d damaged", result.checked, len(result.damaged))
    return result
