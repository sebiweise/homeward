"""File rule filters: patterns, extensions, sizes, ages, order."""

import pytest

from homeward.core.workflow import (
    DAY,
    FileRule,
    RuleAction,
    check_rules,
    first_match,
    format_age,
    format_size,
    parse_age,
    parse_size,
)

NOW = 1_800_000_000.0


def _rule(**kwargs: object) -> FileRule:
    return FileRule(name=str(kwargs.pop("name", "r")), action=RuleAction.IGNORE, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "days"), [("30d", 30), ("2w", 14), ("18m", 540), ("3y", 1095), (" 1 Y ", 365)]
)
def test_parse_age(text: str, days: int) -> None:
    assert parse_age(text) == days


@pytest.mark.parametrize("text", ["3", "y", "-1y", "1.5y", "3 years"])
def test_parse_age_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="age"):
        parse_age(text)


@pytest.mark.parametrize(
    ("text", "size"),
    [("500MB", 500 * 1024**2), ("1.5 GB", 3 * 1024**3 // 2), ("100kb", 102_400),
     ("2g", 2 * 1024**3), ("10", 10), ("1,5MB", 3 * 1024**2 // 2)],
)  # fmt: skip
def test_parse_size(text: str, size: int) -> None:
    assert parse_size(text) == size


@pytest.mark.parametrize("text", ["", "MB", "5 PB", "-1MB"])
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(text)


def test_name_pattern_matches_file_name_case_insensitive() -> None:
    rule = _rule(patterns=("*.mp4",))
    assert rule.matches("Videos/IMG_1.MP4", 1, NOW, NOW)
    assert not rule.matches("Videos/IMG_1.mov", 1, NOW, NOW)


def test_path_pattern_matches_folder_contents() -> None:
    rule = _rule(patterns=("Scans/Alt",))
    assert rule.matches("Scans/Alt/a.pdf", 1, NOW, NOW)
    assert rule.matches("scans/alt/sub/b.pdf", 1, NOW, NOW)
    assert not rule.matches("Scans/Neu/a.pdf", 1, NOW, NOW)
    assert _rule(patterns=("Scans/*.pdf",)).matches("Scans/a.pdf", 1, NOW, NOW)


def test_extensions_ignore_dot_and_case() -> None:
    rule = _rule(extensions=(".MOV", "mp4"))
    assert rule.matches("a.mov", 1, NOW, NOW)
    assert rule.matches("b.Mp4", 1, NOW, NOW)
    assert not rule.matches("c.jpg", 1, NOW, NOW)
    assert not rule.matches("mp4", 1, NOW, NOW)


def test_size_range_is_inclusive() -> None:
    rule = _rule(min_size=10, max_size=20)
    assert [rule.matches("a", s, NOW, NOW) for s in (9, 10, 20, 21)] == [False, True, True, False]
    assert not rule.matches("a", None, NOW, NOW)


def test_age_filters() -> None:
    older = _rule(older_than_days=365)
    newer = _rule(newer_than_days=7)
    assert older.matches("a", 1, NOW - 366 * DAY, NOW)
    assert not older.matches("a", 1, NOW - 364 * DAY, NOW)
    assert not older.matches("a", 1, None, NOW)
    assert newer.matches("a", 1, NOW - 6 * DAY, NOW)
    assert not newer.matches("a", 1, NOW - 8 * DAY, NOW)


def test_all_filters_must_match() -> None:
    rule = _rule(extensions=("mp4",), min_size=100, older_than_days=30)
    old = NOW - 31 * DAY
    assert rule.matches("a.mp4", 100, old, NOW)
    assert not rule.matches("a.mp4", 99, old, NOW)
    assert not rule.matches("a.mov", 100, old, NOW)
    assert not rule.matches("a.mp4", 100, NOW, NOW)


def test_rule_without_filters_matches_everything_unless_disabled() -> None:
    assert _rule().matches("any/thing", None, None, NOW)
    assert not _rule(enabled=False).matches("any/thing", None, None, NOW)


def test_first_matching_rule_wins() -> None:
    keep = FileRule("keep", RuleAction.IGNORE, patterns=("wichtig*",))
    archive = FileRule("archive", RuleAction.ARCHIVE)
    assert first_match([keep, archive], "wichtig.pdf", 1, NOW, NOW) is keep
    assert first_match([keep, archive], "x.pdf", 1, NOW, NOW) is archive
    assert first_match([keep], "x.pdf", 1, NOW, NOW) is None


def test_describe() -> None:
    rule = _rule(patterns=("*.tmp",), extensions=("mp4",), older_than_days=30)
    assert rule.describe() == "*.tmp, .mp4, older than 30 days"
    assert _rule().describe() == "every file"


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ([_rule(name="a"), _rule(name="a")], "duplicate"),
        ([_rule(name=" ")], "name"),
        ([_rule(min_size=2, max_size=1)], "minimum size"),
        ([_rule(older_than_days=30, newer_than_days=10)], "older than"),
    ],
)
def test_check_rules_rejects(rules: list[FileRule], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_rules(rules)


@pytest.mark.parametrize(("days", "text"), [(1095, "3y"), (540, "18m"), (14, "2w"), (10, "10d")])
def test_format_age_round_trips(days: int, text: str) -> None:
    assert format_age(days) == text
    assert parse_age(text) == days


@pytest.mark.parametrize(
    ("size", "text"), [(500 * 1024**2, "500MB"), (1536 * 1024**2, "1536MB"), (1000, "1000B")]
)
def test_format_size_round_trips(size: int, text: str) -> None:
    assert format_size(size) == text
    assert parse_size(text) == size


def test_rule_to_table() -> None:
    rule = FileRule("alt", RuleAction.ARCHIVE, extensions=("pdf",), older_than_days=1095)
    assert rule.to_table() == {
        "name": "alt", "action": "archive", "extensions": ["pdf"], "older_than": "3y",
        "preview": True, "enabled": True,
    }  # fmt: skip
