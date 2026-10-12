"""Reads and safely rewrites the config file for the UI.

Every change is validated with the core config parser before it is written, the
previous file is kept in ``config-backups/`` and the new file replaces the old one
atomically, so a failed or invalid save never leaves a broken or lost config.
"""

import fcntl
import os
import shutil
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from homeward.api import toml_writer
from homeward.api.errors import ApiError
from homeward.core.config import Config, ConfigError, parse_config
from homeward.runtime import Paths

KEEP_BACKUPS = 20
DEFAULT_STATE_DIR = "state"

RawConfig = dict[str, Any]


class ConfigStore:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def read(self) -> RawConfig:
        try:
            with self.paths.config.open("rb") as fh:
                return tomllib.load(fh)
        except FileNotFoundError:
            return {"state_dir": DEFAULT_STATE_DIR}
        except tomllib.TOMLDecodeError as exc:
            raise ApiError("config_invalid", str(exc)) from exc

    def load(self) -> Config:
        return self.parse(self.read())

    def parse(self, raw: RawConfig) -> Config:
        try:
            return parse_config(raw, base=self.paths.config.parent)
        except ConfigError as exc:
            raise ApiError("config_invalid", str(exc)) from exc

    def update(self, change: Callable[[RawConfig], None]) -> Config:
        """Apply ``change`` to the raw config under a lock, validate it and save it."""
        with self._locked():
            raw = self.read()
            change(raw)
            config = self.parse(raw)
            try:
                self._write(raw)
            except PermissionError as exc:
                raise ApiError("config_not_writable", str(exc), status=500) from exc
            return config

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.paths.config.with_name(f".{self.paths.config.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, raw: RawConfig) -> None:
        target = self.paths.config
        text = toml_writer.dumps(raw)
        if target.exists():
            self._backup(target)
        tmp = target.with_name(f".{target.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def _backup(self, target: Path) -> None:
        backup_dir = self.paths.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{target.stem}-{stamp}-{time.time_ns() % 1_000_000_000:09d}{target.suffix}"
        shutil.copy2(target, backup_dir / name)
        backups = sorted(backup_dir.glob(f"{target.stem}-*{target.suffix}"))
        for old in backups[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
