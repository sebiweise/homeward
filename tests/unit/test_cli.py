import pytest

from homeward.cli import main


def test_main_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "homeward" in capsys.readouterr().out
