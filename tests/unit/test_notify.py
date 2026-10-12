from pathlib import Path

from homeward.dsm.notify import Event, notify


def test_notify_calls_synodsmnotify(tmp_path: Path) -> None:
    out = tmp_path / "args"
    binary = tmp_path / "synodsmnotify"
    binary.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\n')
    binary.chmod(0o755)
    assert notify(Event.SYNC_FAILED, "docs", "3", binary=str(binary))
    assert out.read_text().splitlines() == [
        "-c", "SYNO.SDS.Homeward.Instance", "@administrators", "homeward:app:app_name",
        "homeward:notify:sync_failed", "docs", "3",
    ]  # fmt: skip


def test_notify_never_raises(tmp_path: Path) -> None:
    assert not notify(Event.LOGIN_NEEDED, "me", binary=str(tmp_path / "missing"))
    failing = tmp_path / "fail"
    failing.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    failing.chmod(0o755)
    assert not notify(Event.LOGIN_NEEDED, "me", binary=str(failing))
