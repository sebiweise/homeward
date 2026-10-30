"""tools/spksrc_requirements.py keeps the SynoCommunity pins equal to the lock file."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "spksrc_requirements.py"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("spksrc_requirements", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOCK = """\
certifi==2026.10.19 \\
    --hash=sha256:aa
    # via requests
jaraco-classes==3.4.0 \\
    --hash=sha256:bb
pywin32-ctypes==0.2.3 ; sys_platform == 'win32' \\
    --hash=sha256:cc
secretstorage==3.5.0 ; sys_platform == 'linux' \\
    --hash=sha256:dd
"""


def test_lock_pins_leave_out_windows_only_packages(tool: ModuleType) -> None:
    assert tool.lock_pins(LOCK) == {
        "certifi": "2026.10.19",
        "jaraco-classes": "3.4.0",
        "secretstorage": "3.5.0",
    }


def test_update_pins_keeps_names_and_comments(tool: ModuleType) -> None:
    text = "# pure\n\nhomeward==0.3.0\n\ncertifi==2026.7.22\njaraco.classes==3.4.0\n"
    updated, names = tool.update_pins(text, tool.lock_pins(LOCK), "0.4.0")
    assert updated == "# pure\n\nhomeward==0.4.0\n\ncertifi==2026.10.19\njaraco.classes==3.4.0\n"
    assert names == {"homeward", "certifi", "jaraco-classes"}


def test_held_back_pins_follow_the_lock_file_only_up_to_their_major(tool: ModuleType) -> None:
    pins = {"cryptography": "50.0.2"}
    updated, _ = tool.update_pins("cryptography==48.0.1\n", pins, "0.4.0")
    assert updated == "cryptography==48.0.1\n"  # what spksrc can build
    assert tool.held_back_problems(updated) == []
    updated, _ = tool.update_pins("cryptography==48.0.0\n", {"cryptography": "48.0.1"}, "0.4.0")
    assert updated == "cryptography==48.0.1\n"
    (problem,) = tool.held_back_problems("cryptography==50.0.2\n")
    assert "above 48.x" in problem


def test_the_repository_is_in_step(tool: ModuleType) -> None:
    assert tool.main(["--check"]) == 0


def test_check_reports_another_version(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(["--check", "--version", "v99.0.0"]) == 1
    assert "Makefile is out of date" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="not a release version"):
        tool.main(["--check", "--version", "next"])
