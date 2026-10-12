from pathlib import Path

import pytest

from homeward import cli
from homeward.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from homeward.providers.drive_api import DriveApiError
from homeward.providers.fake_drive import FakeDriveApi


def test_main_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "homeward" in capsys.readouterr().out


def _config(tmp_path: Path, provider: str = "folder") -> Path:
    (tmp_path / "nas").mkdir()
    (tmp_path / "cloud").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
state_dir = "state"

[[pair]]
name = "docs"
local = "{tmp_path / "nas"}"
remote = "{tmp_path / "cloud"}"
remote_provider = "{provider}"
mode = "bidirectional"
"""
    )
    return config


def test_sync_dry_run_then_real(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    (tmp_path / "nas" / "a.txt").write_text("a")
    (tmp_path / "cloud" / "b.txt").write_text("b")

    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "upload" in out and "a.txt" in out
    assert "download" in out and "b.txt" in out
    assert "Dry run" in out
    assert not (tmp_path / "cloud" / "a.txt").exists()

    assert main(["sync", "--config", str(config)]) == EXIT_OK
    assert (tmp_path / "cloud" / "a.txt").read_text() == "a"
    assert (tmp_path / "nas" / "b.txt").read_text() == "b"

    assert main(["sync", "--config", str(config), "--pair", "docs"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out


def test_sync_delete_brake_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    for i in range(5):
        (tmp_path / "nas" / f"{i}.txt").write_text(str(i))
    assert main(["sync", "--config", str(config)]) == EXIT_OK
    (tmp_path / "nas" / "0.txt").unlink()

    assert main(["sync", "--config", str(config)]) == EXIT_BLOCKED
    assert "--allow-deletes" in capsys.readouterr().out
    assert (tmp_path / "cloud" / "0.txt").exists()

    assert main(["sync", "--config", str(config), "--allow-deletes"]) == EXIT_OK
    assert not (tmp_path / "cloud" / "0.txt").exists()
    assert (tmp_path / "cloud" / ".homeward-papierkorb").is_dir()


def test_sync_icloud_not_available_yet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path, provider="icloud_drive")
    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_USAGE
    assert "not available yet" in capsys.readouterr().err


def test_sync_with_icloud_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeDriveApi()
    monkeypatch.setattr(cli, "drive_api_factory", lambda pair, config: api)
    config = _config(tmp_path, provider="icloud_drive")
    (tmp_path / "nas" / "a.txt").write_text("a")
    api.put(f"{tmp_path / 'cloud'}/b.txt", "b")

    assert main(["sync", "--config", str(config)]) == EXIT_OK
    assert api.content(f"{tmp_path / 'cloud'}/a.txt") == b"a"
    assert (tmp_path / "nas" / "b.txt").read_text() == "b"
    assert (tmp_path / "state" / "default.drive-folders.json").is_file()


def test_sync_reports_unreachable_icloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeDriveApi()

    def offline(drivewsids: object) -> object:
        raise DriveApiError("HTTP 503")

    monkeypatch.setattr(api, "folders", offline)
    monkeypatch.setattr(cli, "drive_api_factory", lambda pair, config: api)
    config = _config(tmp_path, provider="icloud_drive")
    assert main(["sync", "--config", str(config)]) == EXIT_FAILED
    assert "FAILED: DriveApiError: HTTP 503" in capsys.readouterr().out


def test_sync_purges_old_nas_trash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    old = tmp_path / "nas" / ".homeward-papierkorb" / "2020-01-01"
    old.mkdir(parents=True)
    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_OK
    assert old.exists()  # a dry run changes nothing
    assert main(["sync", "--config", str(config)]) == EXIT_OK
    assert not old.exists()
    assert "Emptied 1 day folder" in capsys.readouterr().out


def test_sync_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync", "--config", str(tmp_path / "missing.toml")]) == EXIT_USAGE
    assert "not found" in capsys.readouterr().err
