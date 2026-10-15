import json
import os
from pathlib import Path

import pytest

from homeward import cli
from homeward.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from homeward.providers.drive_api import DriveApiError
from homeward.providers.fake_drive import FakeDriveApi
from homeward.runtime import Event, Paths, read_events


def _events(tmp_path: Path) -> list[Event]:
    return read_events(Paths(var_dir=tmp_path, config=tmp_path / "config.toml"))


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
    assert "upload" in out
    assert "a.txt" in out
    assert "download" in out
    assert "b.txt" in out
    assert "Dry run" in out
    assert not (tmp_path / "cloud" / "a.txt").exists()

    assert main(["sync", "--config", str(config)]) == EXIT_OK
    assert (tmp_path / "cloud" / "a.txt").read_text() == "a"
    assert (tmp_path / "nas" / "b.txt").read_text() == "b"

    assert main(["sync", "--config", str(config), "--pair", "docs"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out
    summaries = [e.details["counts"] for e in _events(tmp_path) if e.code == "sync_summary"]
    assert summaries == [{}, {"upload": 1, "download": 1}, {"upload": 1, "download": 1}]


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
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job, progress: source)
    config = _photos_config(tmp_path)

    assert main(["photos", "--config", str(config), "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "download  2024/05/a.jpg" in out
    assert "Dry run" in out
    assert not (tmp_path / "photos").exists()

    assert main(["photos", "--config", str(config), "--job", "seb"]) == EXIT_OK
    assert (tmp_path / "photos" / "2024" / "05" / "a.jpg").read_bytes() == b"img"
    assert (tmp_path / "photos" / "Alben" / "Urlaub" / "a.jpg").exists()
    assert (tmp_path / "state" / "seb@example.com.photos.sqlite3").exists()

    capsys.readouterr()
    assert main(["photos", "--config", str(config)]) == EXIT_OK
    assert "nothing changed in iCloud, 1 photos up to date" in capsys.readouterr().out
    summaries = [e.details["counts"] for e in _events(tmp_path) if e.code == "sync_summary"]
    assert summaries == [{"up_to_date": 1}, {"download": 1, "link": 1},
                         {"download": 1, "link": 1}]  # fmt: skip


def test_photos_failed_download_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.fail_keys.add(source.add("a1", "a.jpg").key)
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job, progress: source)

    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_FAILED
    assert "FAILED download" in capsys.readouterr().out
    (summary,) = [e for e in _events(tmp_path) if e.code == "sync_summary"]
    assert summary.details["counts"] == {"download": 1, "failed": 1}
    assert summary.source == "photos:seb"


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
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job, progress: source)

    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_OK
    err = capsys.readouterr().err
    assert "reading iCloud" in err
    assert "1 of 1 files downloaded" in err


def test_photos_progress_file_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.add("a1", "a.jpg")
    progress_file = tmp_path / "progress" / "photos-seb.json"
    seen: list[dict[str, object]] = []
    open_read = source.open_read

    def reading(item: object) -> object:
        seen.append(json.loads(progress_file.read_text()))
        return open_read(item)  # type: ignore[arg-type]

    source.open_read = reading  # type: ignore[assignment,method-assign]
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job, progress: source)

    assert main(["photos", "--config", str(_photos_config(tmp_path))]) == EXIT_OK
    assert seen[0]["phase"] == "downloading"
    assert seen[0]["total"] == 1
    assert seen[0]["pid"] == os.getpid()
    assert not progress_file.exists()  # removed when the run ends


def test_photos_refuses_a_second_run_for_the_same_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    import homeward.cli
    from homeward.providers.fake_photos import FakePhotoSource

    source = FakePhotoSource()
    source.add("a1", "a.jpg")
    monkeypatch.setattr(homeward.cli, "_photo_source", lambda config, job, progress: source)
    config = _photos_config(tmp_path)
    lock = tmp_path / "state" / "seb@example.com.photos.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with lock.open("a") as other_run:
        fcntl.flock(other_run, fcntl.LOCK_EX)
        assert main(["photos", "--config", str(config)]) == EXIT_FAILED
    assert "still running" in capsys.readouterr().err
    assert source.listed == 0

    assert main(["photos", "--config", str(config)]) == EXIT_OK


def test_a_crashing_run_becomes_a_readable_event(tmp_path: Path) -> None:
    import logging

    from homeward.cli import EXIT_FAILED, EXIT_WAITING, _events, _reported
    from homeward.providers.icloud_photos import PhotosNotReady
    from homeward.runtime import Paths, read_events

    class Unavailable(Exception):
        pass

    def crash() -> int:
        try:
            raise KeyError("indexing")
        except KeyError as exc:
            raise Unavailable("Photos service not available") from exc

    def not_ready() -> int:
        raise PhotosNotReady("iCloud is still indexing the photo library")

    config = tmp_path / "config.toml"
    logging.getLogger("homeward").setLevel(logging.INFO)
    with _events(config, "photos:me"):
        assert _reported(crash, "me") == EXIT_FAILED
        assert _reported(not_ready, "me") == EXIT_WAITING
    events = read_events(Paths(var_dir=tmp_path, config=config))
    assert [(e.code, e.level) for e in events] == [("icloud_indexing", "warning"), ("log", "error")]
    assert events[1].message == "the run stopped: Photos service not available ('indexing')"


def test_a_session_apple_rejects_mid_run_asks_for_a_new_sign_in(tmp_path: Path) -> None:
    import logging

    from homeward.cli import EXIT_FAILED, _events, _reported
    from homeward.runtime import Paths, read_events

    class PyiCloudAPIResponseException(Exception):
        def __init__(self, message: str, code: int) -> None:
            super().__init__(message)
            self.code = code

    class PyiCloudServiceUnavailable(Exception):
        pass

    def rejected() -> int:
        try:
            raise PyiCloudAPIResponseException("Authentication required for Account.", 421)
        except PyiCloudAPIResponseException as exc:
            raise PyiCloudServiceUnavailable("Photos service not available") from exc

    config = tmp_path / "config.toml"
    logging.getLogger("homeward").setLevel(logging.INFO)
    with _events(config, "photos:me"):
        assert _reported(rejected, "me") == EXIT_FAILED
    [event] = read_events(Paths(var_dir=tmp_path, config=config))
    assert (event.code, event.details.get("account")) == ("login_required", "me")
    assert "homeward login --account me" in event.message


def test_cli_events_reach_the_log_when_run_as_a_module(tmp_path: Path) -> None:
    # The daemon starts runs with `python -m homeward.cli`, where __name__ is __main__.
    import subprocess
    import sys

    config = tmp_path / "config.toml"
    config.write_text(
        f'state_dir = "state"\n[[photos]]\nname = "me"\naccount = "me"\n'
        f'apple_id = "me@example.com"\ntarget = "{tmp_path / "photos"}"\n'
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    run = subprocess.run(
        [sys.executable, "-m", "homeward.cli", "photos", "--config", str(config)],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 1, run.stderr
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [e["code"] for e in events] == ["login_required"]


def test_sigterm_ends_a_sync_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    import shutil
    import signal

    from homeward.cli import EXIT_CANCELLED
    from homeward.providers import nas

    config = _config(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / "nas" / name).write_text(name * 1000)
    copies: list[int] = []
    copy = shutil.copyfileobj

    def copy_then_sigterm(src, dst, length=0):  # type: ignore[no-untyped-def]
        copies.append(1)
        if len(copies) == 2:
            dst.write(src.read(10))  # half a file, then the cancel arrives
            os.kill(os.getpid(), signal.SIGTERM)
        return copy(src, dst, length)

    handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(nas.shutil, "copyfileobj", copy_then_sigterm)
    assert main(["sync", "--config", str(config)]) == EXIT_CANCELLED
    assert signal.getsignal(signal.SIGTERM) == handler  # restored after the run
    assert "cancelled" in capsys.readouterr().err
    cloud = tmp_path / "cloud"
    assert [p.name for p in cloud.iterdir()] == ["a.txt"]  # no half or temporary file

    monkeypatch.undo()
    assert main(["sync", "--config", str(config)]) == EXIT_OK
    assert sorted(p.name for p in cloud.iterdir()) == ["a.txt", "b.txt", "c.txt"]
    assert "2 upload" in capsys.readouterr().out  # a.txt was kept from the first run


def test_the_worst_exit_code_wins() -> None:
    from homeward.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, EXIT_WAITING, _worst

    assert _worst(EXIT_OK, EXIT_WAITING) == EXIT_WAITING
    assert _worst(EXIT_WAITING, EXIT_FAILED) == EXIT_FAILED
    assert _worst(EXIT_BLOCKED, EXIT_WAITING) == EXIT_BLOCKED
