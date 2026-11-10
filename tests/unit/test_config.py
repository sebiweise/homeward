import tomllib
from pathlib import Path

import pytest

from homeward.core.config import AccountConfig, ConfigError, load_config, parse_config
from homeward.core.models import SyncMode
from homeward.core.workflow import RuleAction

VALID = """
state_dir = "state"

[[pair]]
name = "docs"
local = "/volume1/homes/alice/Docs"
remote = "Documents"
mode = "bidirectional"
beta = true
exclude = ["*.tmp"]
max_delete_count = 10
trash_days = 7

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
    assert docs.trash_days == 7
    assert config.pair("photos-out").trash_days == 30
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
        (
            {"name": "x", "local": "/a", "remote": "r", "mode": "upload", "trash_days": -1},
            "trash_days",
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


def test_accounts() -> None:
    config = parse_config(
        {
            "account": [{"name": "me", "apple_id": " me@example.com "}, {"name": "x"}],
            "pair": [
                {"name": "a", "local": "/a", "remote": "r", "mode": "upload", "account": "me"}
            ],
        }
    )
    assert config.accounts == (AccountConfig("me", "me@example.com"), AccountConfig("x"))
    assert config.account("x") == AccountConfig("x") and config.account("nope") is None
    assert config.apple_id_for("me") == "me@example.com"
    assert config.apple_id_for("me", "other@example.com") == "other@example.com"
    assert config.apple_id_for("x") is None  # no Apple ID in its table
    assert config.apple_id_for("y@example.com") == "y@example.com"
    assert config.apple_id_for("nope") is None
    assert parse_config({}).accounts == ()


@pytest.mark.parametrize(
    ("accounts", "message"),
    [
        ({"name": "me"}, "list"),
        (["me"], "must be a table"),
        ([{"apple_id": "me@example.com"}], "'name' is required"),
        ([{"name": " "}], "must not be empty"),
        ([{"name": "me", "apple_id": 1}], "wrong type"),
        ([{"name": "me"}, {"name": "me"}], "duplicate account names: me"),
    ],
)
def test_invalid_accounts(accounts: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config({"account": accounts})


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"local": "/volume1/docs/#recycle/x"}, "recycle bin"),
        ({"local": "/volume1/@eaDir"}, "DSM system folder"),
        ({"local": "/volume1/docs/../x"}, "'\\.\\.'"),
        ({"local": "/srv"}, "state folder"),  # contains /srv/homeward/state
        ({"local": "/srv/homeward/state/sessions"}, "state folder"),
        ({"remote": "/volume1/.homeward-papierkorb", "remote_provider": "folder"}, "trash"),
    ],
)
def test_folders_written_by_hand_are_checked(entry: dict[str, object], message: str) -> None:
    pair = {"name": "x", "local": "/volume1/docs", "remote": "r", "mode": "upload"} | entry
    with pytest.raises(ConfigError, match=message):
        parse_config({"state_dir": "/srv/homeward/state", "pair": [pair]})


def test_folder_checks_leave_cloud_paths_and_good_folders_alone() -> None:
    pair = {"name": "x", "local": "/volume1/docs", "remote": "#recycle", "mode": "upload"}
    photos = {"name": "p", "target": "/volume1/photo/@eaDir"}
    assert parse_config({"state_dir": "/srv/state", "pair": [pair]}).pairs[0].remote == "#recycle"
    with pytest.raises(ConfigError, match="photos 'p': target"):
        parse_config({"state_dir": "/srv/state", "photos": [photos]})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_unknown_pair() -> None:
    config = parse_config({})
    with pytest.raises(ConfigError, match="no sync pair"):
        config.pair("x")


PHOTOS = """
state_dir = "/state"

[[photos]]
name = "alice"
account = "alice"
apple_id = "alice@example.com"
target = "/volume1/photo/Alice"

[[photos]]
name = "anna"
account = "anna"
target = "/volume1/photo/Anna"
libraries = ["personal"]
albums = false
folder_scheme = "{year}"
shared_folder = "Familie"
source_folders = false
hidden = true
xmp = false
hidden_folder = "Privat"
workers = 1
"""


def test_photos_jobs() -> None:
    import tomllib

    from homeward.core.photos import Library

    config = parse_config(tomllib.loads(PHOTOS))
    alice = config.photos_job("alice")
    assert alice.apple_id == "alice@example.com"
    assert alice.libraries == (Library.PERSONAL, Library.SHARED, Library.SHARED_ALBUMS)
    assert alice.albums
    assert alice.videos
    assert alice.live_videos
    assert alice.raw_alternative
    assert alice.edited
    assert alice.favorites
    assert alice.xmp
    assert not alice.hidden
    assert alice.workers == 4
    assert alice.layout.favorites_folder == "Favoriten"
    assert alice.layout.scheme == "{year}/{month}"
    assert alice.layout.source_folders
    anna = config.photos_job("anna")
    assert anna.apple_id is None
    assert not anna.albums
    assert anna.libraries == (Library.PERSONAL,)
    assert anna.layout.scheme == "{year}"
    assert anna.layout.shared_folder == "Familie"
    assert not anna.layout.source_folders
    assert anna.hidden
    assert not anna.xmp
    assert anna.layout.hidden_folder == "Privat"
    assert anna.workers == 1
    assert config.photo_state_path("anna") == Path("/state/anna.photos.sqlite3")
    with pytest.raises(ConfigError, match="no photos job"):
        config.photos_job("nope")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('libraries = ["icloud"]', "libraries may only contain"),
        ("libraries = []", "must not be empty"),
        ('folder_scheme = "{album}"', "folder_scheme"),
        ('albums = "yes"', "true or false"),
        ('source_folders = "no"', "true or false"),
        ("workers = 0", "from 1 to 8"),
        ("workers = 9", "from 1 to 8"),
        ('workers = "4"', "from 1 to 8"),
    ],
)
def test_photos_invalid(extra: str, message: str) -> None:
    import tomllib

    text = f'[[photos]]\nname = "p"\ntarget = "/t"\n{extra}\n'
    raw = tomllib.loads(text)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_photos_relative_target_and_duplicates() -> None:
    with pytest.raises(ConfigError, match="absolute"):
        parse_config({"photos": [{"name": "p", "target": "rel"}]})
    with pytest.raises(ConfigError, match="duplicate photos"):
        parse_config({"photos": [{"name": "p", "target": "/a"}, {"name": "p", "target": "/b"}]})


def test_intervals() -> None:
    config = parse_config({"pair": [], "photos": []})
    assert config.interval_minutes == 60
    text = VALID.replace("trash_days = 7\n", "trash_days = 7\ninterval_minutes = 15\n")
    config = parse_config({**tomllib.loads(text), "daemon": {"interval_minutes": 30}})
    assert config.interval_minutes == 30
    assert [pair.interval_minutes for pair in config.pairs] == [15, None]


@pytest.mark.parametrize(
    "data",
    [
        {"daemon": {"interval_minutes": 4}},
        {"daemon": {"interval_minutes": 10081}},
        {"daemon": []},
        {"photos": [{"name": "p", "target": "/volume1/p", "interval_minutes": "15"}]},
    ],
)
def test_invalid_intervals(data: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match=r"interval_minutes|daemon"):
        parse_config(data)


def test_time_window_and_rate() -> None:
    text = VALID.replace(
        "trash_days = 7\n", 'trash_days = 7\nwindow = "22:00-06:00"\nmax_rate_kb = 500\n'
    )
    config = parse_config(
        {**tomllib.loads(text), "photos": [{"name": "p", "target": "/a", "max_rate_kb": 0}]}
    )
    docs, other = config.pairs
    assert (str(docs.window), docs.max_rate_kb) == ("22:00-06:00", 500)
    assert (other.window, other.max_rate_kb) == (None, None)
    assert config.photos[0].max_rate_kb is None  # 0 means no limit


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"window": "22-6"}, "HH:MM"),
        ({"window": 22}, "window"),
        ({"max_rate_kb": -1}, "max_rate_kb"),
        ({"max_rate_kb": "500"}, "max_rate_kb"),
        ({"max_rate_kb": True}, "max_rate_kb"),
    ],
)
def test_invalid_limits(entry: dict[str, object], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config({"photos": [{"name": "p", "target": "/a", **entry}]})


RULES = """
[[pair]]
name = "docs"
local = "/volume1/docs"
remote = "Documents"
mode = "download"
archive_limit = 50

[[pair.rule]]
name = "Videos"
action = "ignore"
extensions = ["mp4", ".MOV"]
min_size = "500MB"

[[pair.rule]]
name = "Alt"
action = "archive"
patterns = ["Scans/*"]
older_than = "3y"
preview = false
"""


def test_pair_rules() -> None:
    pair = parse_config(tomllib.loads(RULES)).pair("docs")
    videos, old = pair.rules
    assert pair.archive_limit == 50
    assert videos.action is RuleAction.IGNORE
    assert videos.extensions == ("mp4", ".MOV")
    assert videos.min_size == 500 * 1024**2
    assert videos.preview  # archive rules start in preview unless switched off
    assert old.action is RuleAction.ARCHIVE
    assert old.patterns == ("Scans/*",)
    assert old.older_than_days == 1095
    assert not old.preview


def test_pair_without_rules() -> None:
    pair = parse_config(tomllib.loads(VALID)).pair("docs")
    assert pair.rules == ()
    assert pair.archive_limit == 200


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('mode = "download"', 'mode = "upload"'), "archive rules need a download"),
        (('action = "ignore"', 'action = "move"'), "action must be one of"),
        (('older_than = "3y"', 'older_than = "drei Jahre"'), "age 'drei Jahre'"),
        (('min_size = "500MB"', "min_size = 500"), "'min_size' must be a text"),
        (('min_size = "500MB"', 'min_size = "500MB"\nsize = 1'), "unknown keys size"),
        (('name = "Alt"', 'name = "Videos"'), "duplicate rule names"),
        (("archive_limit = 50", "archive_limit = -1"), "archive_limit"),
        (('extensions = ["mp4", ".MOV"]', 'extensions = ["mp4", ""]'), "extensions must be"),
    ],
)
def test_pair_rules_invalid(change: tuple[str, str], message: str) -> None:
    text = RULES.replace(*change)
    with pytest.raises(ConfigError, match=message):
        parse_config(tomllib.loads(text))


@pytest.mark.parametrize("target", ["var/requests", "var/login/x", "var", "@appdata/homeward"])
def test_folders_are_checked_where_their_symlinks_lead(tmp_path: Path, target: str) -> None:
    # A share user may turn a synced folder into a symlink after it was saved; the
    # daemon checks again on every load. Synced content in var/requests could forge
    # "run now" requests, @appdata is where DSM really keeps the package's var folder.
    (tmp_path / target).mkdir(parents=True)
    (tmp_path / "share").mkdir()
    link = tmp_path / "share" / "x"
    link.symlink_to(tmp_path / target, target_is_directory=True)
    for local in (link, link / "sub"):
        pair = {"name": "x", "local": str(local), "remote": "r", "mode": "upload"}
        with pytest.raises(ConfigError, match="local"):
            parse_config({"pair": [pair]}, base=tmp_path / "var")


def test_symlinks_to_ordinary_folders_are_accepted(tmp_path: Path) -> None:
    real = tmp_path / "share" / "real"
    real.mkdir(parents=True)
    (tmp_path / "share" / "alias").symlink_to(real, target_is_directory=True)
    for local in (real, tmp_path / "share" / "alias"):
        pair = {"name": "x", "local": str(local), "remote": "r", "mode": "upload"}
        config = parse_config({"pair": [pair]}, base=tmp_path / "var")
        assert config.pairs[0].local == local


def test_folders_in_homeward_var_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEWARD_VAR", str(tmp_path / "pkgvar"))
    pair = {"name": "x", "local": str(tmp_path / "pkgvar" / "cancel"), "remote": "r",
            "mode": "upload"}  # fmt: skip
    with pytest.raises(ConfigError, match="Homeward's own files"):
        parse_config({"pair": [pair]}, base=tmp_path / "var")
