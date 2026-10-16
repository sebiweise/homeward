"""Contract tests: the pyicloud internals Homeward relies on are still there.

They run against the installed pyicloud (CI installs the pinned version from
requirements/icloud.txt). When one fails after raising the pin, the matching
function in ``providers/pyicloud_compat.py`` needs an update.
"""

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from homeward.providers import pyicloud_compat as compat

pytest.importorskip("pyicloud")

ZONE = {"zoneName": "PrimarySync", "ownerRecordName": "_owner", "zoneType": "REGULAR_CUSTOM_ZONE"}


def _module(name: str) -> Any:
    return compat._module(name)


def _assigns(cls: type, attribute: str) -> bool:
    """Whether the class sets this instance attribute (``self.x = ...`` or ``self.x: T``)."""
    source = inspect.getsource(cls)
    return f"self.{attribute} =" in source or f"self.{attribute}:" in source


def _params(function: Any) -> set[str]:
    return set(inspect.signature(function).parameters)


def test_cloudkit_requests_can_be_built() -> None:
    request = compat.zone_id_request(ZONE)
    assert request.zoneName == "PrimarySync"
    changes = compat.zone_changes_request(SimpleNamespace(zone_id=ZONE), "token")
    assert changes.syncToken == "token"
    assert changes.zoneID.zoneName == "PrimarySync"


def test_records_are_recognised() -> None:
    models = _module(compat.MODELS)
    assert inspect.isclass(models.CKRecord)
    assert not compat.is_record({"recordName": "x"})


def test_photo_libraries_keep_their_client_and_zone() -> None:
    service = _module(compat.SERVICE)
    assert _assigns(service.BasePhotoLibrary, "_client")
    assert _assigns(service.BasePhotoLibrary, "zone_id")
    assert _assigns(service.BasePhotoLibrary, "asset_type")
    assert {"service", "zone_id", "client", "scope"} <= _params(service.PhotoLibrary.__init__)
    assert compat.cloudkit_client(SimpleNamespace()) is None


def test_the_photos_service_exposes_its_root_library_and_client() -> None:
    service = _module(compat.SERVICE)
    assert _assigns(service.PhotosService, "_root_library")
    assert isinstance(inspect.getattr_static(service.PhotosService, "private_client"), property)
    assert compat.primary_zone_name() == "PrimarySync"
    assert compat.is_shared_library_zone("SharedSync-1234")
    assert not compat.is_shared_library_zone("PrimarySync")


def test_the_cloudkit_client_calls_keep_their_parameters() -> None:
    client = _module("pyicloud.services.photos_cloudkit.client").PhotosCloudKitClient
    assert "zone_req" in _params(client.iter_changes)
    assert {"record_names", "zone_id"} <= _params(client.lookup)
    assert {"query", "zone_id", "results_limit"} <= _params(client.query)
    assert callable(client.zones_list)


def test_mappers_queries_and_xmp_export_exist() -> None:
    assert compat.master_asset_pairs([]) == ({}, [])
    assert compat.indexing_state_query() is not None
    materialize = _module(compat.MATERIALIZE)
    assert callable(materialize.build_xmp_metadata)
    assert callable(materialize._render_xmp_xml)


def test_the_drive_service_keeps_its_document_root_and_upload_token() -> None:
    drive = _module("pyicloud.services.drive").DriveService
    assert _assigns(drive, "_document_root")
    assert callable(drive._get_token_from_cookie)
    fake = SimpleNamespace(_document_root="https://docws", _get_token_from_cookie=lambda: {"t": 1})
    assert compat.drive_document_root(fake) == "https://docws"
    assert compat.drive_upload_token(fake) == {"t": 1}
