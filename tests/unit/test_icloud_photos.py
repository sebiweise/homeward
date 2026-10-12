"""The pyicloud Photos adapter, driven by stand-ins shaped like pyicloud 2.7 objects."""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from homeward.core.photos import Library
from homeward.providers.icloud_photos import ICloudPhotosSource


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


@dataclass
class Album:
    fullname: str
    photos: list[Asset]


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


class Response:
    def __init__(self, data: bytes) -> None:
        self.raw = io.BytesIO(data)

    def raise_for_status(self) -> None:
        pass


class Session:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.requests.append((url, kwargs))
        return Response(url.encode())


class Photos:
    def __init__(self, libraries: dict[str, Library_]) -> None:
        self.libraries = libraries


class Service:
    def __init__(self, libraries: dict[str, Library_]) -> None:
        self.photos = Photos(libraries)
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
    source = ICloudPhotosSource(make_service())
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
    assert source.cursor() == "root=t1|shared:SharedSync-1=t9"
    service.photos.libraries["root"].token = "t2"
    assert source.cursor() == "root=t2|shared:SharedSync-1=t9"


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
