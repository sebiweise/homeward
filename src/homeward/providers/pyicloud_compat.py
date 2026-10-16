"""Every place Homeward reaches into pyicloud's private API, in one module.

pyicloud has no public API for CloudKit change feeds, record lookups, opening one
library zone at a time, XMP export or streamed Drive uploads, so the adapters use
its internals. They do it only through these functions. ``pyproject.toml`` pins
pyicloud to 2.7.x, and ``tests/unit/test_pyicloud_compat.py`` checks each of them
against the installed pyicloud: after raising the pin, that test shows what moved.

pyicloud is imported lazily, so the rest of Homeward (and its tests with fakes)
runs without it.
"""

import importlib
from typing import Any

MODELS = "pyicloud.common.cloudkit.models"
MAPPERS = "pyicloud.services.photos_cloudkit.mappers"
MATERIALIZE = "pyicloud.services.photos_cloudkit.materialize"
QUERIES = "pyicloud.services.photos_cloudkit.queries"
SERVICE = "pyicloud.services.photos_cloudkit.service"


def _module(name: str) -> Any:
    return importlib.import_module(name)


# --- CloudKit models -------------------------------------------------------------------


def is_record(value: object) -> bool:
    """Whether ``value`` is a CloudKit record (not an error or a tombstone)."""
    return isinstance(value, _module(MODELS).CKRecord)


def zone_id_request(zone_id: dict[str, Any]) -> Any:
    return _module(MODELS).CKZoneIDReq(**zone_id)


def zone_changes_request(library: Any, since: str) -> Any:
    """The request for a library's change feed since the sync token ``since``."""
    models = _module(MODELS)
    return models.CKZoneChangesZoneReq(
        zoneID=models.CKZoneID(**library.zone_id), syncToken=since, reverse=False
    )


# --- Photos service --------------------------------------------------------------------


def cloudkit_client(library: Any) -> Any | None:
    """The typed CloudKit client of a photo library (None with a legacy session)."""
    return getattr(library, "_client", None)


def root_library(photos: Any) -> Any:
    """The personal library, without opening the other zones like ``libraries``."""
    return photos._root_library


def primary_zone_name() -> str:
    return str(_module(SERVICE).PRIMARY_ZONE["zoneName"])


def is_shared_library_zone(zone_name: str) -> bool:
    return bool(_module(SERVICE)._is_shared_library_zone_name(zone_name))


def open_library(photos: Any, zone_id: dict[str, Any], client: Any, *, shared: bool) -> Any:
    """Open one library zone; raises like pyicloud while Apple is still indexing it."""
    return _module(SERVICE).PhotoLibrary(
        photos, zone_id=zone_id, client=client, scope="shared-library" if shared else "private"
    )


def master_asset_pairs(records: list[Any]) -> tuple[dict[str, Any], list[Any]]:
    """Asset records by master record name, and the master records."""
    pairs: tuple[dict[str, Any], list[Any]] = _module(MAPPERS).master_asset_pairs(records)
    return pairs


def indexing_state_query() -> Any:
    return _module(QUERIES).check_indexing_state_query()


def xmp_metadata(asset_record: Any) -> Any | None:
    return _module(MATERIALIZE).build_xmp_metadata(asset_record)


def render_xmp(metadata: Any) -> Any:
    """The XMP sidecar of ``xmp_metadata`` as an ElementTree element."""
    return _module(MATERIALIZE)._render_xmp_xml(metadata)


# --- Drive service ---------------------------------------------------------------------


def drive_document_root(drive: Any) -> str:
    """The document service URL of pyicloud's Drive service (downloads, uploads)."""
    return str(drive._document_root)


def drive_upload_token(drive: Any) -> dict[str, Any]:
    """The request parameter that authorises an upload, from the session cookie."""
    token: dict[str, Any] = drive._get_token_from_cookie()
    return token
