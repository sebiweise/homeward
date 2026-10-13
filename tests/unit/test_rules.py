from datetime import date

import pytest

from homeward.core.planner import conflict_name
from homeward.core.rules import is_excluded


@pytest.mark.parametrize(
    "path",
    [
        "@eaDir/thumb.jpg",
        "a/b/.DS_Store",
        "#recycle/x.txt",
        "docs/Thumbs.db",
        "docs/._report.pdf",
        "Folder/Icon\r",
        "docs/~$report.docx",
        "docs/.~lock.calc.ods#",
        ".Spotlight-V100/store.db",
    ],
)
def test_default_excludes(path: str) -> None:
    assert is_excluded(path)


def test_regular_file_not_excluded() -> None:
    assert not is_excluded("Documents/report.pdf")


def test_extra_patterns() -> None:
    assert is_excluded("build/cache.tmp", ["*.tmp"])
    assert not is_excluded("build/cache.txt", ["*.tmp"])


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("f.txt", "f (Konflikt NAS 2026-10-11).txt"),
        ("docs/Plan v2.pages", "docs/Plan v2 (Konflikt NAS 2026-10-11).pages"),
        ("Makefile", "Makefile (Konflikt NAS 2026-10-11)"),
        (".bashrc", ".bashrc (Konflikt NAS 2026-10-11)"),
        ("backup.tar.gz", "backup.tar (Konflikt NAS 2026-10-11).gz"),
    ],
)
def test_conflict_name(path: str, expected: str) -> None:
    assert conflict_name(path, date(2026, 10, 11)) == expected


def test_conflict_name_counts_up() -> None:
    taken = {"f (Konflikt NAS 2026-10-11).txt", "f (Konflikt NAS 2026-10-11 2).txt"}
    assert conflict_name("f.txt", date(2026, 10, 11), taken) == "f (Konflikt NAS 2026-10-11 3).txt"
