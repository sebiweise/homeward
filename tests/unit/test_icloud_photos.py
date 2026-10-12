"""The pyicloud Photos adapter, driven by stand-ins shaped like pyicloud 2.7 objects."""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from homeward.core.photos import FAVORITES_ALBUM, Library
from homeward.providers.icloud_photos import RENEW_AFTER, ICloudPhotosSource


@dataclass
class Resource:
    filename: str
    url: str | None
    size: int | None = 3
    checksum: str | None = "ck"


@dataclass
class Asset:
    id: str
    filename: str = "IMG.HEIC"
    item_type: str = "image"
    is_live_photo: bool = False
    asset_date: datetime = datetime(2024, 5, 17, 10, 0)
    resources: dict[str, Resource] = field(default_factory=dict)
    asset_record: dict[str, Any] | None = None
    FILE_TYPE_EXTENSIONS: ClassVar[dict[str, str]] = {
        "public.jpeg": ".JPG",
        "com.apple.quicktime-movie": ".MOV",
    }


@dataclass
class StreamAsset:
    """A legacy shared-album asset: ``versions`` dicts instead of ``resources``."""

    id: str
    filename: str
    versions: dict[str, dict[str, Any]]
    item_type: str = "image"
    is_live_photo: bool = False
    asset_date: datetime = datetime(2019, 7, 1, 8, 0, tzinfo=UTC)


@dataclass
class Album:
    fullname: str
    photos: list[Asset]

    def get(self, asset_id: str) -> Asset | None:
        return next((a for a in self.photos if a.id == asset_id), None)


class SmartPhotoAlbum(Album):
    pass


class PhotoAlbumFolder(Album):
    pass


class PhotoAlbum(Album):
    pass


class Library_:
    def __init__(self, photos: list[Asset], albums: list[Album], token: str = "t1") -> None:
        self.all = Album("All Photos", photos)
        self.albums = albums
        self.token = token

    def sync_cursor(self) -> str:
        return self.token


class Gone(Exception):
    """How pyicloud reports Apple's 410 for an expired download URL."""

    code = 410


class Response:
    def __init__(self, data: bytes) -> None:
        self.raw = io.BytesIO(data)

    def raise_for_status(self) -> None:
        pass


class Session:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.expired: set[str] = set()

    def get(self, url: str, **kwargs: Any) -> Response:
        self.requests.append((url, kwargs))
        if url in self.expired:
            raise Gone("Apple no longer serves cvws.icloud-content.com/B/x/${f}")
        return Response(url.encode())


@dataclass
class SharedPhotoStreamAlbum:
    id: str
    fullname: str
    photos: list[StreamAsset]
    _album_ctag: str | None = "ctag1"


class Photos:
    def __init__(
        self, libraries: dict[str, Library_], streams: list[SharedPhotoStreamAlbum] | None = None
    ) -> None:
        self.libraries = libraries
        self.streams = streams

    @property
    def shared_streams(self) -> list[SharedPhotoStreamAlbum]:
        if self.streams is None:
            raise RuntimeError("Shared photo streams are not available for this account")
        return self.streams


class Service:
    def __init__(
        self, libraries: dict[str, Library_], streams: list[SharedPhotoStreamAlbum] | None = None
    ) -> None:
        self.photos = Photos(libraries, streams)
        self.session = Session()


def image(asset_id: str, live: bool = False) -> Asset:
    resources = {"original": Resource(f"{asset_id}.HEIC", f"https://cdn/{asset_id}")}
    if live:
        resources["original_video"] = Resource(f"{asset_id}.MOV", f"https://cdn/{asset_id}-v")
    return Asset(asset_id, f"{asset_id}.HEIC", is_live_photo=live, resources=resources)


def make_service() -> Service:
    a, b = image("a"), image("b", live=True)
    video = Asset(
        "v", "v.MOV", item_type="movie", resources={"original": Resource("v.MOV", "https://cdn/v")}
    )
    root = Library_(
        [a, b, video],
        [
            SmartPhotoAlbum("Favorites", [a]),
            PhotoAlbumFolder("Trips", [a]),
            PhotoAlbum("Trips/Rome", [a, b]),
            PhotoAlbum("Cats", [a]),
        ],
    )
    shared = Library_([image("s")], [], token="t9")
    return Service({"root": root, "shared": Library_([], []), "shared:SharedSync-1": shared})


def test_lists_personal_and_shared_with_albums_and_live_photos() -> None:
    source = ICloudPhotosSource(make_service(), [Library.PERSONAL, Library.SHARED])
    items = {item.key: item for item in source.list_items()}

    assert set(items) == {
        "personal/a/original",
        "personal/b/original",
        "personal/b/original_video",
        "personal/v/original",
        "shared/s/original",
    }
    assert items["personal/a/original"].albums == ("Cats", "Trips/Rome")  # user albums only
    assert items["personal/b/original_video"].filename == "b.MOV"
    assert items["shared/s/original"].library is Library.SHARED
    assert items["personal/a/original"].created == datetime(2024, 5, 17, 10, 0, tzinfo=UTC)


def test_filters() -> None:
    source = ICloudPhotosSource(
        make_service(), [Library.PERSONAL], albums=False, videos=True, live_videos=False
    )
    items = {item.key: item for item in source.list_items()}
    assert set(items) == {"personal/a/original", "personal/b/original", "personal/v/original"}
    assert items["personal/a/original"].albums == ()

    no_videos = ICloudPhotosSource(make_service(), [Library.PERSONAL], videos=False)
    assert {i.key for i in no_videos.list_items()} == {"personal/a/original", "personal/b/original"}


def test_cursor_combines_library_tokens() -> None:
    service = make_service()
    source = ICloudPhotosSource(service)
    assert source.cursor() == "root=t1|shared:SharedSync-1=t9|options=11111101"
    service.photos.libraries["root"].token = "t2"
    assert source.cursor() == "root=t2|shared:SharedSync-1=t9|options=11111101"
    switched = ICloudPhotosSource(service, hidden=True).cursor()
    assert switched is not None and switched.endswith("options=11111111")


def test_cursor_none_when_a_library_has_no_token() -> None:
    service = make_service()

    def broken() -> str:
        raise RuntimeError("no token")

    service.photos.libraries["root"].sync_cursor = broken  # type: ignore[method-assign]
    assert ICloudPhotosSource(service).cursor() is None


def test_open_read_streams_the_listed_url() -> None:
    service = make_service()
    source = ICloudPhotosSource(service, [Library.PERSONAL])
    item = next(i for i in source.list_items() if i.key == "personal/a/original")
    with source.open_read(item) as stream:
        assert stream.read() == b"https://cdn/a"
    _url, kwargs = service.session.requests[0]
    assert kwargs["stream"] is True and kwargs["timeout"] > 0


def test_open_read_unknown_item() -> None:
    source = ICloudPhotosSource(make_service())
    item = next(iter(ICloudPhotosSource(make_service()).list_items()))
    with pytest.raises(KeyError):
        source.open_read(item)


def test_asset_without_download_url_is_skipped() -> None:
    broken = Asset("x", resources={"original": Resource("x.HEIC", None)})
    service = Service({"root": Library_([broken], [])})
    assert list(ICloudPhotosSource(service).list_items()) == []


def test_shared_photo_in_personal_album(caplog: pytest.LogCaptureFixture) -> None:
    shared_photo, ghost = image("s"), image("ghost")
    root = Library_([image("a")], [PhotoAlbum("Family", [shared_photo, ghost])])
    service = Service({"root": root, "shared:SharedSync-1": Library_([shared_photo], [])})

    with caplog.at_level("INFO", logger="homeward"):
        items = {item.key: item for item in ICloudPhotosSource(service).list_items()}
    assert items["shared/s/original"].albums == ("Family",)
    assert "1 album photos were not found" in caplog.text


def record(**fields: Any) -> dict[str, Any]:
    return {"fields": {name: {"value": value} for name, value in fields.items()}}


def rich_service() -> Service:
    """A favorite RAW+JPEG shot with an edit, a hidden photo and a shared album."""
    raw = Asset(
        "r",
        "r.DNG",
        resources={
            "original": Resource("r.DNG", "https://cdn/r", checksum="ck-r"),
            "alternative": Resource("r.JPG", "https://cdn/r-alt", checksum="ck-alt"),
        },
        asset_record=record(
            isFavorite=1,
            adjustmentType="com.apple.photo",
            resJPEGFullRes={"downloadURL": "https://cdn/r-edit", "size": 7},
            resJPEGFullFileType="public.jpeg",
            resJPEGFullFingerprint="ck-edit",
        ),
    )
    plain = Asset(
        "p", "p.HEIC", resources={"original": Resource("p.HEIC", "https://cdn/p")}, asset_record={}
    )
    secret = Asset(
        "h", "h.HEIC", resources={"original": Resource("h.HEIC", "https://cdn/h")}, asset_record={}
    )
    root = Library_([raw, plain], [SmartPhotoAlbum("Hidden", [secret]), PhotoAlbum("Hidden", [])])
    stream = StreamAsset(
        "x", "x.JPG", {"original": {"url": "https://cdn/x", "size": 9, "filename": "x.JPG"}}
    )
    album = SharedPhotoStreamAlbum("guid1", "Hochzeit", [stream])
    return Service({"root": root}, [album])


def fake_xmp(asset: Any) -> bytes | None:
    return f"<xmp id='{asset.id}'/>".encode() if asset.id != "p" else None


def test_all_features() -> None:
    source = ICloudPhotosSource(rich_service(), hidden=True, xmp_builder=fake_xmp)
    items = {item.key: item for item in source.list_items()}

    assert set(items) == {
        "personal/r/original",
        "personal/r/alternative",
        "personal/r/edited",
        "personal/r/xmp",
        "personal/p/original",
        "personal/h/original",
        "personal/h/xmp",
        "shared_albums/guid1:x/original",
    }
    assert items["personal/r/alternative"].filename == "r.JPG"
    edited = items["personal/r/edited"]
    assert (edited.filename, edited.size, edited.checksum) == ("r (bearbeitet).JPG", 7, "ck-edit")
    assert FAVORITES_ALBUM in items["personal/r/original"].albums
    assert items["personal/h/original"].hidden
    sidecar = items["personal/r/xmp"]
    assert sidecar.filename == "r.DNG.xmp" and sidecar.sidecar_for == "original"
    assert sidecar.albums == ()
    shared = items["shared_albums/guid1:x/original"]
    assert shared.collection == "Hochzeit" and shared.size == 9

    with source.open_read(sidecar) as stream:
        assert stream.read() == b"<xmp id='r'/>"
    with source.open_read(items["personal/r/edited"]) as stream:
        assert stream.read() == b"https://cdn/r-edit"
    assert source.cursor() == "root=t1|shared_albums=ctag1|options=11111111"


def test_features_can_be_switched_off() -> None:
    source = ICloudPhotosSource(
        rich_service(),
        [Library.PERSONAL],
        raw_alternative=False,
        edited=False,
        favorites=False,
        hidden=False,
        xmp=False,
    )
    items = {item.key: item for item in source.list_items()}
    assert set(items) == {"personal/r/original", "personal/p/original"}
    assert items["personal/r/original"].albums == ()


def test_shared_albums_without_ctag_force_a_listing() -> None:
    service = rich_service()
    service.photos.streams[0]._album_ctag = None  # type: ignore[index]
    assert ICloudPhotosSource(service).cursor() is None


def test_xmp_failure_skips_only_the_sidecar() -> None:
    def broken(asset: Any) -> bytes | None:
        raise ValueError("bad plist")

    source = ICloudPhotosSource(rich_service(), [Library.PERSONAL], xmp_builder=broken)
    keys = {item.key for item in source.list_items()}
    assert "personal/r/original" in keys and "personal/r/xmp" not in keys


def test_pyicloud_xmp_skips_photos_without_metadata() -> None:
    pytest.importorskip("pyicloud")
    from types import SimpleNamespace

    from homeward.providers.icloud_photos import pyicloud_xmp

    assert pyicloud_xmp(SimpleNamespace(asset_record=record(assetDate=1700000000000))) is None
    data = pyicloud_xmp(SimpleNamespace(asset_record=record(isFavorite=1)))
    assert data is not None and b"<xmp:Rating>5</xmp:Rating>" in data


def by_name(library: Library_, asset_ids: list[str], master_ids: list[str]) -> list[Asset]:
    """Stands in for a CloudKit records/lookup: the library's current assets by id."""
    return [asset for asset in library.all.photos if asset.id in asset_ids]


def test_expired_url_is_fetched_again() -> None:
    service = make_service()
    source = ICloudPhotosSource(service, [Library.PERSONAL], lookup=by_name)
    item = next(i for i in source.list_items() if i.key == "personal/a/original")
    service.session.expired.add("https://cdn/a")
    service.photos.libraries["root"].all.photos[0].resources["original"].url = "https://cdn/a2"

    with source.open_read(item) as stream:
        assert stream.read() == b"https://cdn/a2"
    assert [url for url, _ in service.session.requests] == ["https://cdn/a", "https://cdn/a2"]


def test_expired_edited_url_is_fetched_again() -> None:
    service = rich_service()
    source = ICloudPhotosSource(service, [Library.PERSONAL], lookup=by_name)
    item = next(i for i in source.list_items() if i.key == "personal/r/edited")
    service.session.expired.add("https://cdn/r-edit")
    asset = service.photos.libraries["root"].all.photos[0]
    assert asset.asset_record is not None
    asset.asset_record["fields"]["resJPEGFullRes"]["value"]["downloadURL"] = "https://cdn/r-new"

    with source.open_read(item) as stream:
        assert stream.read() == b"https://cdn/r-new"


def test_other_download_errors_are_not_retried() -> None:
    service = make_service()
    source = ICloudPhotosSource(service, [Library.PERSONAL])
    item = next(i for i in source.list_items() if i.key == "personal/a/original")

    def broken(url: str, **kwargs: Any) -> Response:
        raise ConnectionError("reset")

    service.session.get = broken  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        source.open_read(item)


def test_expired_url_of_a_deleted_photo_fails() -> None:
    service = make_service()
    source = ICloudPhotosSource(service, [Library.PERSONAL], lookup=by_name)
    item = next(i for i in source.list_items() if i.key == "personal/a/original")
    service.session.expired.add("https://cdn/a")
    del service.photos.libraries["root"].all.photos[0]

    with pytest.raises(Gone):
        source.open_read(item)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_old_links_are_renewed_in_one_lookup_per_library() -> None:
    service, clock = rich_service(), Clock()
    lookups: list[tuple[list[str], list[str]]] = []
    root = service.photos.libraries["root"]

    def lookup(library: Any, asset_ids: list[str], master_ids: list[str]) -> list[Asset]:
        assert library is root
        lookups.append((asset_ids, master_ids))
        fresh = Asset(
            "r", "r.DNG", resources={"original": Resource("r.DNG", "https://cdn/r-new")},
            asset_record=record(resJPEGFullRes={"downloadURL": "https://cdn/r-edit-new"}),
        )  # fmt: skip
        return [fresh]

    source = ICloudPhotosSource(service, [Library.PERSONAL], lookup=lookup, clock=clock)
    items = {item.key: item for item in source.list_items()}
    wanted = [
        items["personal/r/original"],
        items["personal/r/edited"],
        items["personal/p/original"],
    ]

    source.prepare(wanted)
    assert lookups == []  # still fresh

    clock.now += RENEW_AFTER
    source.prepare(wanted)
    assert lookups == [(["r", "p"], ["r", "p"])]
    with source.open_read(items["personal/r/original"]) as stream:
        assert stream.read() == b"https://cdn/r-new"
    with source.open_read(items["personal/r/edited"]) as stream:
        assert stream.read() == b"https://cdn/r-edit-new"
    with source.open_read(items["personal/p/original"]) as stream:  # not returned: kept as is
        assert stream.read() == b"https://cdn/p"

    source.prepare(wanted[:2])
    assert len(lookups) == 1  # renewed links count as fresh again


def test_failed_renewal_keeps_the_listed_links(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()

    def broken(library: Any, asset_ids: list[str], master_ids: list[str]) -> list[Asset]:
        raise RuntimeError("lookup refused")

    source = ICloudPhotosSource(make_service(), [Library.PERSONAL], lookup=broken, clock=clock)
    item = next(i for i in source.list_items() if i.key == "personal/a/original")
    clock.now += RENEW_AFTER
    with caplog.at_level("WARNING", logger="homeward"):
        source.prepare([item])
    assert "could not renew download links" in caplog.text
    with source.open_read(item) as stream:
        assert stream.read() == b"https://cdn/a"


def test_pyicloud_lookup_builds_assets_with_new_urls() -> None:
    pytest.importorskip("pyicloud")
    from pyicloud.common.cloudkit.models import CKLookupResponse
    from pyicloud.services.photos_cloudkit.service import PhotoAsset

    from homeward.providers.icloud_photos import pyicloud_lookup

    response = CKLookupResponse.model_validate(
        {
            "records": [
                {
                    "recordName": "M1",
                    "recordType": "CPLMaster",
                    "fields": {
                        "resOriginalRes": {
                            "type": "ASSETID",
                            "value": {"downloadURL": "https://cdn/new", "size": 3},
                        },
                        "filenameEnc": {"type": "ENCRYPTED_BYTES", "value": "SU1HLkhFSUM="},
                    },
                },
                {
                    "recordName": "A1",
                    "recordType": "CPLAsset",
                    "fields": {"masterRef": {"type": "REFERENCE", "value": {"recordName": "M1"}}},
                },
                {"recordName": "A2", "serverErrorCode": "NOT_FOUND", "reason": "gone"},
            ]
        }
    )

    class Client:
        def lookup(self, *, record_names: list[str], zone_id: Any) -> Any:
            assert record_names == ["A1", "A2", "M1", "M2"]
            assert zone_id.zoneName == "PrimarySync"
            return response

    class Lib:
        _client = Client()
        zone_id: ClassVar[dict[str, str]] = {"zoneName": "PrimarySync"}
        service = None
        asset_type = PhotoAsset

    [asset] = pyicloud_lookup(Lib(), ["A1", "A2"], ["M1", "M2"])
    assert asset.id == "A1" and asset.resources["original"].url == "https://cdn/new"


class BadGateway(Exception):
    code = 503


def test_cdn_hiccup_is_retried_once() -> None:
    service, pauses = make_service(), []
    source = ICloudPhotosSource(service, [Library.PERSONAL], sleep=pauses.append)
    item = next(i for i in source.list_items() if i.key == "personal/a/original")
    real_get, failures = service.session.get, [BadGateway("Bad Gateway")]

    def flaky(url: str, **kwargs: Any) -> Response:
        if failures:
            raise failures.pop()
        return real_get(url, **kwargs)

    service.session.get = flaky  # type: ignore[method-assign]
    with source.open_read(item) as stream:
        assert stream.read() == b"https://cdn/a"
    assert len(pauses) == 1

    failures.extend([BadGateway("again"), BadGateway("Bad Gateway")])
    with pytest.raises(BadGateway):
        source.open_read(item)
