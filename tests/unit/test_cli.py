from pathlib import Path

import pytest

from homeward.cli import EXIT_BLOCKED, EXIT_OK, EXIT_USAGE, main


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
beta = true
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


def test_sync_shows_renames(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    for i in range(3):
        (tmp_path / "nas" / f"{i}.txt").write_text(str(i))
    assert main(["sync", "--config", str(config)]) == EXIT_OK
    (tmp_path / "nas" / "0.txt").rename(tmp_path / "nas" / "zero.txt")
    capsys.readouterr()

    assert main(["sync", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "(bidirectional, beta)" in out
    assert "0.txt -> zero.txt" in out
    assert "1 of the deletes are renames" in out
    assert (tmp_path / "cloud" / "zero.txt").read_text() == "0"
    assert not (tmp_path / "cloud" / "0.txt").exists()


def test_sync_icloud_not_available_yet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path, provider="icloud_drive")
    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_USAGE
    assert "not available yet" in capsys.readouterr().err


def test_sync_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync", "--config", str(tmp_path / "missing.toml")]) == EXIT_USAGE
    assert "not found" in capsys.readouterr().err
