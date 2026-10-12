from pathlib import Path

import pytest

from homeward.core.config import ConfigError, load_config, parse_config
from homeward.core.models import SyncMode

VALID = """
state_dir = "state"

[[pair]]
name = "docs"
local = "/volume1/homes/seb/Docs"
remote = "Documents"
mode = "bidirectional"
beta = true
exclude = ["*.tmp"]
max_delete_count = 10

[[pair]]
name = "photos-out"
account = "anna"
local = "/volume1/out"
remote = "/volume1/mirror"
remote_provider = "folder"
mode = "upload"
"""


def test_load_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID)
    config = load_config(path)
    docs = config.pair("docs")
    assert docs.mode is SyncMode.BIDIRECTIONAL
    assert docs.beta
    assert docs.exclude == ("*.tmp",)
    assert docs.delete_brake.max_count == 10
    assert docs.delete_brake.max_ratio == 0.10
    assert config.pair("photos-out").remote_provider == "folder"
    assert config.state_path("anna") == tmp_path / "state" / "anna.sqlite3"


@pytest.mark.parametrize(
    ("pair", "message"),
    [
        ({"local": "/a", "remote": "r", "mode": "upload"}, "'name' is required"),
        ({"name": "x", "local": "/a", "remote": "r", "mode": "sideways"}, "mode must be"),
        ({"name": "x", "local": "rel", "remote": "r", "mode": "upload"}, "absolute"),
        ({"name": "x", "local": "/a", "remote": "r", "mode": "upload", "exclude": [1]}, "exclude"),
        (
            {"name": "x", "local": "/a", "remote": "r", "mode": "upload", "remote_provider": "s3"},
            "remote_provider",
        ),
        ({"name": "x", "local": "/a", "remote": 5, "mode": "upload"}, "wrong type"),
        ({"name": "x", "local": "/a", "remote": "r", "mode": "bidirectional"}, "in beta"),
        (
            {"name": "x", "local": "/a", "remote": "r", "mode": "bidirectional", "beta": "yes"},
            "true or false",
        ),
    ],
)
def test_invalid_pairs(pair: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config({"pair": [pair]})


def test_duplicate_names() -> None:
    pair = {"name": "x", "local": "/a", "remote": "r", "mode": "upload"}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"pair": [pair, pair]})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_unknown_pair() -> None:
    with pytest.raises(ConfigError, match="no sync pair"):
        parse_config({}).pair("x")
