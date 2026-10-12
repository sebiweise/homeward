"""Minimal TOML writer for the config file (the standard library only reads TOML).

Supports what the config uses: strings, integers, floats, booleans, arrays of
those, tables and arrays of tables. Comments in the file are not preserved.
"""

import json
import math
import re
from collections.abc import Mapping
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def dumps(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _table(data, (), lines)
    return "\n".join(lines).strip() + "\n"


def _table(data: Mapping[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    tables: list[tuple[str, Mapping[str, Any]]] = []
    arrays: list[tuple[str, list[Mapping[str, Any]]]] = []
    for key, value in data.items():
        if isinstance(value, Mapping):
            tables.append((key, value))
        elif _is_table_array(value):
            arrays.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in tables:
        path = (*prefix, key)
        lines.extend(["", f"[{_dotted(path)}]"])
        _table(value, path, lines)
    for key, items in arrays:
        path = (*prefix, key)
        for item in items:
            lines.extend(["", f"[[{_dotted(path)}]]"])
            _table(item, path, lines)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(_key(part) for part in path)


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic string escapes.
    return json.dumps(value, ensure_ascii=False)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TOML config values must be finite numbers")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")
