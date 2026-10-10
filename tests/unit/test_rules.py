import pytest

from homeward.core.rules import is_excluded


@pytest.mark.parametrize(
    "path",
    ["@eaDir/thumb.jpg", "a/b/.DS_Store", "#recycle/x.txt", "docs/Thumbs.db"],
)
def test_default_excludes(path: str) -> None:
    assert is_excluded(path)


def test_regular_file_not_excluded() -> None:
    assert not is_excluded("Documents/report.pdf")


def test_extra_patterns() -> None:
    assert is_excluded("build/cache.tmp", ["*.tmp"])
    assert not is_excluded("build/cache.txt", ["*.tmp"])
