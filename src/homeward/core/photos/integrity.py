"""The weekly check that the backed-up photos on the NAS are still whole.

A file whose size no longer matches what was downloaded (cut off, overwritten,
damaged) is only reported here; nothing is moved yet. The run that follows reads
the whole library and replaces the file only when iCloud still has the photo: the
damaged copy then goes to the trash, never deleted, and its album links (hard links
to the same damaged data) are linked to the new download. A photo iCloud no longer
has stays where it is, damaged or not: the NAS copy is all there is. That includes
photos deleted in iCloud since the last complete reading, which earlier runs could
not yet mark as gone.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from homeward.core.photo_state import PhotoStateDB

log = logging.getLogger("homeward.core.photos")

INTEGRITY_DAYS = 7


@dataclass(slots=True)
class IntegrityResult:
    checked: int = 0
    damaged: list[str] = field(default_factory=list)  # paths whose size does not match
    keys: set[str] = field(default_factory=set)  # their keys, for the plan to replace


def integrity_due(state: PhotoStateDB, job: str, now: float) -> bool:
    last = state.integrity_checked_at(job)
    return last is None or now - last >= INTEGRITY_DAYS * 86400


def check_integrity(job: str, root: Path, state: PhotoStateDB) -> IntegrityResult:
    """Compare every backed-up photo with the size it was downloaded with.

    Changes nothing on the NAS: the damaged photos iCloud still has are replaced by
    the plan of the same run (see ``build_photo_plan(damaged=...)``)."""
    result = IntegrityResult()
    for record in state.records(job).values():
        if record.size is None:
            continue
        path = root / record.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue  # missing files are downloaded again by the plan anyway
        except OSError as exc:
            log.warning("integrity check: %s cannot be read: %s", record.path, exc)
            continue
        result.checked += 1
        if size == record.size:
            continue
        if record.gone_at is not None:
            log.warning(
                "integrity check: %s has %d bytes instead of %d; iCloud no longer has it, "
                "so it stays as it is", record.path, size, record.size,
                extra={"event": "integrity_damaged"},
            )  # fmt: skip
            continue
        log.info(
            "integrity check: %s has %d bytes instead of %d; it is downloaded again "
            "if iCloud still has it", record.path, size, record.size,
        )  # fmt: skip
        result.damaged.append(record.path)
        result.keys.add(record.key)
    log.info("integrity check: %d photos checked, %d damaged", result.checked, len(result.damaged))
    return result
