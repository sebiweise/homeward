"""File rules: filters (name, extension, size, age) that pick an action for a file.

A job holds an ordered list of rules. For each file the first rule whose filters all
match decides what happens to it; a file no rule matches is synced as usual.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import PurePosixPath

DAY = 24 * 60 * 60
DEFAULT_ARCHIVE_LIMIT = 200

_AGE_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}
_SIZE_UNITS = {"": 1, "b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
_AGE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)
_SIZE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([kmgt]?b?)\s*$", re.IGNORECASE)


class RuleAction(StrEnum):
    """What a matching rule does with a file."""

    IGNORE = "ignore"  # not synced in either direction
    ARCHIVE = "archive"  # kept on the NAS, then removed from iCloud (into its trash)


def parse_age(text: str) -> int:
    """``"3y"`` / ``"18m"`` / ``"2w"`` / ``"30d"`` to days (a month counts 30 days,
    a year 365)."""
    match = _AGE.match(text)
    if not match:
        raise ValueError(f'age {text!r} must be a number with d, w, m or y, e.g. "3y"')
    return int(match.group(1)) * _AGE_UNITS[match.group(2).lower()]


def parse_size(text: str) -> int:
    """``"500MB"`` / ``"1.5 GB"`` / ``"100kb"`` to bytes (binary units)."""
    match = _SIZE.match(text)
    if not match:
        raise ValueError(f'size {text!r} must be a number with KB, MB, GB or TB, e.g. "500MB"')
    unit = match.group(2).lower()
    if unit and not unit.endswith("b"):
        unit += "b"
    return int(float(match.group(1).replace(",", ".")) * _SIZE_UNITS[unit])


def format_age(days: int) -> str:
    """The shortest text :func:`parse_age` reads back as ``days``."""
    for unit in ("y", "m", "w"):
        if days and days % _AGE_UNITS[unit] == 0:
            return f"{days // _AGE_UNITS[unit]}{unit}"
    return f"{days}d"


def format_size(size: int) -> str:
    """The shortest exact text :func:`parse_size` reads back as ``size``."""
    for unit in ("TB", "GB", "MB", "KB"):
        factor = _SIZE_UNITS[unit.lower()]
        if size and size % factor == 0:
            return f"{size // factor}{unit}"
    return f"{size}B"


@dataclass(frozen=True, slots=True)
class FileRule:
    """One rule: all set filters must match. An unset filter matches every file.

    ``patterns`` are globs matched against the file name and, when they hold a ``/``,
    against the whole path (``*.mp4``, ``Videos/*``). ``extensions`` are compared
    without the dot and case. Sizes are bytes, ages days since the file's date
    (modification time on Drive). ``preview`` keeps an archive rule from removing
    anything: the run only reports what it would archive.
    """

    name: str
    action: RuleAction
    patterns: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    older_than_days: int | None = None
    newer_than_days: int | None = None
    preview: bool = True
    enabled: bool = True

    def matches(self, path: str, size: int | None, mtime: float | None, now: float) -> bool:
        if not self.enabled:
            return False
        if self.patterns and not any(_glob(path, p) for p in self.patterns):
            return False
        if self.extensions and _extension(path) not in {_norm_ext(e) for e in self.extensions}:
            return False
        if not _in_range(size, self.min_size, self.max_size):
            return False
        return self._age_matches(mtime, now)

    def _age_matches(self, mtime: float | None, now: float) -> bool:
        if self.older_than_days is None and self.newer_than_days is None:
            return True
        if mtime is None:
            return False
        age = now - mtime
        if self.older_than_days is not None and age < self.older_than_days * DAY:
            return False
        return self.newer_than_days is None or age < self.newer_than_days * DAY

    def to_table(self) -> dict[str, object]:
        """The rule as a config table (the inverse of the config parser)."""
        table: dict[str, object] = {"name": self.name, "action": self.action.value}
        if self.patterns:
            table["patterns"] = list(self.patterns)
        if self.extensions:
            table["extensions"] = list(self.extensions)
        for key, size in (("min_size", self.min_size), ("max_size", self.max_size)):
            if size is not None:
                table[key] = format_size(size)
        for key, days in (
            ("older_than", self.older_than_days),
            ("newer_than", self.newer_than_days),
        ):
            if days is not None:
                table[key] = format_age(days)
        table["preview"] = self.preview
        table["enabled"] = self.enabled
        return table

    def describe(self) -> str:
        """A short English summary for the log, e.g. ``*.mp4, older than 1095 days``."""
        parts = [*self.patterns]
        parts += [f".{_norm_ext(e)}" for e in self.extensions]
        if self.min_size is not None:
            parts.append(f"at least {self.min_size} bytes")
        if self.max_size is not None:
            parts.append(f"at most {self.max_size} bytes")
        if self.older_than_days is not None:
            parts.append(f"older than {self.older_than_days} days")
        if self.newer_than_days is not None:
            parts.append(f"newer than {self.newer_than_days} days")
        return ", ".join(parts) or "every file"


def first_match(
    rules: Iterable[FileRule], path: str, size: int | None, mtime: float | None, now: float
) -> FileRule | None:
    """The rule that decides ``path``, or None when the file is synced as usual."""
    return next((rule for rule in rules if rule.matches(path, size, mtime, now)), None)


def check_rules(rules: Sequence[FileRule]) -> None:
    """Reject rules that can never match or whose action cannot work."""
    names = [rule.name for rule in rules]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate rule names: {', '.join(duplicates)}")
    for rule in rules:
        if not rule.name.strip():
            raise ValueError("every rule needs a name")
        low, high = rule.min_size, rule.max_size
        if low is not None and high is not None and low > high:
            raise ValueError(f"rule {rule.name!r}: the minimum size is above the maximum")
        if (
            rule.older_than_days is not None
            and rule.newer_than_days is not None
            and rule.older_than_days >= rule.newer_than_days
        ):
            raise ValueError(f"rule {rule.name!r}: 'older than' must be less than 'newer than'")


def _glob(path: str, pattern: str) -> bool:
    """Case-insensitive, like iCloud Drive: ``*.mp4`` also matches ``IMG_1.MP4``."""
    path, pattern = path.casefold(), pattern.strip().casefold()
    if "/" in pattern:
        pattern = pattern.strip("/")
        return fnmatch(path, pattern) or fnmatch(path, pattern + "/*")
    return fnmatch(PurePosixPath(path).name, pattern)


def _norm_ext(ext: str) -> str:
    return ext.strip().lstrip(".").casefold()


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lstrip(".").casefold()


def _in_range(size: int | None, low: int | None, high: int | None) -> bool:
    if low is None and high is None:
        return True
    if size is None:
        return False
    return (low is None or size >= low) and (high is None or size <= high)
