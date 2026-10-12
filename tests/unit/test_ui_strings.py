"""The DSM UI strings stay complete in both languages."""

import json
import re
from pathlib import Path

from homeward.api.handlers import photos
from homeward.dsm.notify import Event
from homeward.runtime import EVENT_CODES, Level

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "package" / "ui"
SRC = ROOT / "src" / "homeward"
# "section:key" literals; a key ending in "_" is a prefix completed at runtime
_KEY = re.compile(r'"([a-z]+:[a-z0-9]+(?:_[a-z0-9]+)*)"')


def _strings(lang: str) -> dict[str, str]:
    result, section = {}, ""
    for line in (UI / "texts" / lang / "strings").read_text(encoding="utf-8").splitlines():
        if head := re.match(r"^\[(.+)\]$", line.strip()):
            section = head[1]
        elif entry := re.match(r'^([\w.-]+)="(.*)"$', line.strip()):
            result[f"{section}:{entry[1]}"] = entry[2]
        else:
            assert not line.strip(), f"{lang}: unexpected line {line!r}"
    return result


def test_languages_have_the_same_keys() -> None:
    assert _strings("enu").keys() == _strings("ger").keys()


def test_every_key_used_by_the_ui_exists() -> None:
    used = set()
    for name in ("app.js", "index.html"):
        used |= set(re.findall(_KEY, (UI / name).read_text(encoding="utf-8")))
    config = json.loads((UI / "config").read_text(encoding="utf-8"))
    for entry in config["desktop.js"].values():
        used |= {entry["title"], *entry.get("preloadTexts", [])}
    assert used - _strings("enu").keys() == set()


def test_every_api_error_code_has_a_message() -> None:
    codes = set()
    for source in (SRC / "api").rglob("*.py"):
        codes |= set(re.findall(r'ApiError\(\s*"([a-z_0-9]+)"', source.read_text()))
    assert codes
    assert {f"error:{code}" for code in codes} - _strings("enu").keys() == set()


def test_modes_states_and_notifications_have_texts() -> None:
    keys = _strings("enu").keys()
    expected = {f"mode:{m}" for m in ("upload", "download", "bidirectional")}
    expected |= {f"state:{s}" for s in ("signed_in", "needs_2fa", "signed_out", "unavailable")}
    expected |= {f"notify:{event}" for event in Event}
    expected |= {f"photos:{key}" for key in [*photos.SWITCHES, *photos.FOLDERS]}
    expected |= {f"photos:lib_{lib}" for lib in photos.LIBRARIES}
    expected |= {f"event:{code}" for code in EVENT_CODES}
    expected |= {f"event:level_{level}" for level in Level} | {
        f"log:level_{level}" for level in Level
    }
    expected |= {f"event:src_{kind}" for kind in ("photos", "pair", "account", "service")}
    assert expected - keys == set()


def test_no_stylesheet_leaks_into_the_dsm_desktop() -> None:
    # DSM injects a third-party app's ui/style.css into the whole desktop page.
    assert not (UI / "style.css").exists()
    assert 'href="homeward.css"' in (UI / "index.html").read_text(encoding="utf-8")
