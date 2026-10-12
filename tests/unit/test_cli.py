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


def test_icloud_sign_in_uses_the_stored_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, str, Path]] = []
    real_open_service = cli.open_service

    def open_service(account: str, apple_id: str, state_dir: Path) -> object:
        calls.append((account, apple_id, state_dir))
        return real_open_service(account, apple_id, state_dir)  # no session stored yet

    monkeypatch.setattr(cli, "open_service", open_service)
    config = _config(tmp_path, provider="icloud_drive")
    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_USAGE
    assert "set apple_id" in capsys.readouterr().err
    config.write_text(config.read_text() + 'apple_id = "seb@example.com"\n')
    assert main(["sync", "--config", str(config), "--dry-run"]) == EXIT_USAGE
    assert "homeward login --account default" in capsys.readouterr().err
    assert calls == [("default", "seb@example.com", tmp_path / "state")]


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


def _photos_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
state_dir = "state"

[[photos]]
name = "seb"
account = "seb@example.com"
target = "{tmp_path / "photos"}"
"""
    )
    return config


def test_photos_dry_run_then_real(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.add(
        "a1", "a.jpg", b"img", created=datetime(2024, 5, 3, 12, tzinfo=UTC), albums=("Urlaub",)
    )
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job: source)
    config = _photos_config(tmp_path)

    assert main(["photos", "--config", str(config), "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "download  2024/05/a.jpg" in out and "Dry run" in out
    assert not (tmp_path / "photos").exists()

    assert main(["photos", "--config", str(config), "--job", "seb"]) == EXIT_OK
    assert (tmp_path / "photos" / "2024" / "05" / "a.jpg").read_bytes() == b"img"
    assert (tmp_path / "photos" / "Alben" / "Urlaub" / "a.jpg").exists()
    assert (tmp_path / "state" / "seb@example.com.photos.sqlite3").exists()

    capsys.readouterr()
    assert main(["photos", "--config", str(config)]) == EXIT_OK
    assert "nothing changed in iCloud, 1 photos up to date" in capsys.readouterr().out


def test_photos_failed_download_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.fail_keys.add(source.add("a1", "a.jpg").key)
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job: source)

    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_FAILED
    assert "FAILED download" in capsys.readouterr().out


def test_photos_without_session_asks_for_login(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_FAILED
    assert "homeward login --account seb@example.com" in capsys.readouterr().err


def test_photos_none_configured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text('state_dir = "state"\n')
    assert main(["photos", "--config", str(config)]) == EXIT_USAGE
    assert "no photos jobs" in capsys.readouterr().err


def test_login_flow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeward.auth.twofa
    from tests.unit.test_auth import FakeService

    monkeypatch.setattr(homeward.auth.twofa, "pyicloud_service", lambda: FakeService)
    monkeypatch.setenv("HOMEWARD_PASSWORD", "secret")
    monkeypatch.setattr("builtins.input", lambda prompt: "123456")
    config = _photos_config(tmp_path)

    assert main(["login", "--config", str(config), "--account", "seb@example.com"]) == EXIT_OK
    assert "is logged in" in capsys.readouterr().out
    assert (tmp_path / "state" / "sessions" / "seb@example.com" / "trusted").exists()

    monkeypatch.setattr("builtins.input", lambda prompt: "000000")
    assert (
        main(["login", "--config", str(config), "--account", "x", "--apple-id", "x@y"])
        == EXIT_FAILED
    )
    assert "rejected" in capsys.readouterr().err
    assert main(["login", "--config", str(config), "--account", "nobody"]) == EXIT_USAGE


def test_photos_progress_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.add("a1", "a.jpg")
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job: source)

    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_OK
    err = capsys.readouterr().err
    assert "reading iCloud" in err
    assert "1 of 1 files downloaded" in err


def test_photos_refuses_a_second_run_for_the_same_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.add("a1", "a.jpg")
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job: source)
    config = _photos_config(tmp_path)
    lock = tmp_path / "state" / "seb@example.com.photos.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with lock.open("a") as other_run:
        fcntl.flock(other_run, fcntl.LOCK_EX)
        assert main(["photos", "--config", str(config)]) == EXIT_FAILED
    assert "still running" in capsys.readouterr().err
    assert source.listed == 0

    assert main(["photos", "--config", str(config)]) == EXIT_OK
