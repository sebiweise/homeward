from pathlib import Path

import pytest

from homeward.api.migrate import main
from homeward.api.store import ConfigStore
from homeward.runtime import Paths

OLD = """
state_dir = "state"

[[pair]]
name = "docs"
local = "/volume1/docs"
remote = "Docs"
mode = "bidirectional"

[[pair]]
name = "out"
local = "/volume1/out"
remote = "Out"
mode = "upload"
"""


def _paths(tmp_path: Path) -> Paths:
    return Paths(var_dir=tmp_path, config=tmp_path / "config.toml")


def test_old_bidirectional_pairs_get_the_beta_flag(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config.write_text(OLD)
    store = ConfigStore(paths)

    assert store.migrate() == ["docs"]

    config = store.load()  # valid again
    assert config.pair("docs").beta
    assert not config.pair("out").beta
    assert list(paths.backup_dir.iterdir())  # the old file is kept
    assert store.migrate() == []  # nothing left to do


def test_migrate_without_config(tmp_path: Path) -> None:
    assert ConfigStore(_paths(tmp_path)).migrate() == []
    assert not (tmp_path / "config.toml").exists()


def test_main_reads_the_package_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "config.toml").write_text(OLD)
    monkeypatch.setenv("HOMEWARD_VAR", str(tmp_path))
    monkeypatch.delenv("HOMEWARD_CONFIG", raising=False)

    assert main() == 0
    assert "docs" in capsys.readouterr().out
    assert "beta = true" in (tmp_path / "config.toml").read_text()
