"""iCloud Photos backup (iCloud -> NAS only): plan and apply one run of a photos job.

Every original lands once below ``<target>/<scheme>/`` (Year/Month by default); the
shared library and the shared albums go below their own folders unless
``PhotoLayout.source_folders`` is off (for a job that backs up one of them alone).
Album folders hold hard links to those files, so a photo in several albums takes its
disk space once.

By default a backup never deletes: photos removed in iCloud stay on the NAS, a changed
original moves the old file to the trash first, and only album links (never the last
copy of a photo) are removed when a photo leaves an album. With ``mirror_deletes`` (or
a cleanup on request) photos deleted in iCloud go to the trash too.

The package is split into ``model`` (types), ``plan`` (what a run does) and
``executor`` (doing it); everything public is importable from here.
"""

from homeward.core.photos.executor import PhotoExecutor, PhotoOutcome, backup_photos
from homeward.core.photos.model import (
    DEFAULT_WORKERS,
    FAVORITES_ALBUM,
    FULL_LISTING_HOURS,
    MAX_WORKERS,
    PREPARE_BATCH,
    PROGRESS_EVERY,
    SCHEME_FIELDS,
    Library,
    PhotoAction,
    PhotoActionKind,
    PhotoFailure,
    PhotoItem,
    PhotoLayout,
    PhotoPlan,
    PhotoRunResult,
    PhotoSource,
    check_scheme,
    safe_component,
    safe_name,
    safe_relative,
)
from homeward.core.photos.plan import PhotoRules, build_photo_plan

__all__ = [
    "DEFAULT_WORKERS",
    "FAVORITES_ALBUM",
    "FULL_LISTING_HOURS",
    "MAX_WORKERS",
    "PREPARE_BATCH",
    "PROGRESS_EVERY",
    "SCHEME_FIELDS",
    "Library",
    "PhotoAction",
    "PhotoActionKind",
    "PhotoExecutor",
    "PhotoFailure",
    "PhotoItem",
    "PhotoLayout",
    "PhotoOutcome",
    "PhotoPlan",
    "PhotoRules",
    "PhotoRunResult",
    "PhotoSource",
    "backup_photos",
    "build_photo_plan",
    "check_scheme",
    "safe_component",
    "safe_name",
    "safe_relative",
]
