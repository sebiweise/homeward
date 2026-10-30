"""Keep the SynoCommunity package (spksrc/) in step with the tested lock file.

The .spk from tools/build_spk.py bundles exactly requirements/icloud.txt; the spksrc
package installs the same dependencies from spksrc/spk/homeward/src/requirements-*.txt
(compiled ones cross-compiled, pure ones from PyPI). This tool copies the versions of
the lock file into those two files and keeps the Homeward version the same in all
three places spksrc needs it (``homeward==`` and both Makefiles).

    python tools/spksrc_requirements.py                    # update from the lock file
    python tools/spksrc_requirements.py --version 0.4.0    # also set the version
    python tools/spksrc_requirements.py --check            # CI: fail on any drift
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements" / "icloud.txt"
SRC = ROOT / "spksrc" / "spk" / "homeward" / "src"
PURE = SRC / "requirements-pure.txt"
CROSS = SRC / "requirements-crossenv.txt"
SPK_MAKEFILE = ROOT / "spksrc" / "spk" / "homeward" / "Makefile"
CROSS_MAKEFILE = ROOT / "spksrc" / "cross" / "homeward" / "Makefile"

# Packages spksrc cannot cross-compile in the lock file's version yet: their pin
# follows the lock file only up to this major version. Remove an entry once the
# "SynoCommunity package" workflow builds the newer version.
HELD_BACK: dict[str, tuple[int, str]] = {
    # From 49 on, cryptography-cffi compiles its C part in a Rust build script that
    # does not get spksrc's Python include path ("Python.h: No such file or
    # directory"). 48.x is also the newest any spksrc package uses.
    "cryptography": (48, "newer versions do not cross-compile in spksrc yet"),
}

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def normalize(name: str) -> str:
    """PEP 503: ``SecretStorage``, ``jaraco.classes`` and ``typing_extensions`` as pip
    compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(text: str) -> dict[str, str]:
    """Normalized name -> version of everything the lock file installs on Linux."""
    pins = {}
    for line in text.splitlines():
        match = _PIN.match(line)
        if match is None:
            continue
        marker = line.partition(";")[2]
        if "sys_platform == 'win32'" in marker:
            continue  # never installed on a NAS
        pins[normalize(match["name"])] = match["version"]
    return pins


def update_pins(text: str, pins: dict[str, str], version: str) -> tuple[str, set[str]]:
    """``text`` with every pin set to the lock file's version (``homeward`` to
    ``version``, a ``HELD_BACK`` one only up to its major version); also returns
    the normalized names it pins."""
    lines, names = [], set()
    for line in text.splitlines():
        match = _PIN.match(line)
        if match is not None:
            name = normalize(match["name"])
            names.add(name)
            new = version if name == "homeward" else pins.get(name)
            if new is not None and name in HELD_BACK and _major(new) > HELD_BACK[name][0]:
                new = None  # keep the version spksrc can build
            if new is not None:
                line = f"{match['name']}=={new}{line[match.end() :]}"  # keeps a marker
        lines.append(line)
    return "\n".join(lines) + "\n", names


def held_back_problems(text: str, pins: dict[str, str]) -> list[str]:
    """Held-back pins above the major version spksrc can build, or, while the lock
    file is above it, below that major version (older ones may not satisfy the
    other dependencies, e.g. pyicloud's ``cryptography>=44``)."""
    problems = []
    for line in text.splitlines():
        match = _PIN.match(line)
        if match is None or normalize(match["name"]) not in HELD_BACK:
            continue
        name, version = normalize(match["name"]), match["version"]
        cap, reason = HELD_BACK[name]
        if _major(version) > cap:
            problems.append(f"{name}=={version} is above {cap}.x: {reason}")
        elif name in pins and _major(pins[name]) > cap and _major(version) < cap:
            problems.append(f"{name}=={version} is below {cap}.x, the newest spksrc builds")
    return problems


def _major(version: str) -> int:
    return int(version.split(".", 1)[0])


def set_make_var(text: str, var: str, value: str) -> str:
    result, count = re.subn(rf"^{var} = .*$", f"{var} = {value}", text, flags=re.M)
    if count != 1:
        raise SystemExit(f"{var} not found exactly once")
    return result


def current_version() -> str:
    for line in PURE.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match is not None and normalize(match["name"]) == "homeward":
            return match["version"]
    raise SystemExit(f"{PURE.relative_to(ROOT)}: no homeward== line")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--version", help="Homeward version to set, e.g. 0.4.0 (or v0.4.0)")
    parser.add_argument("--check", action="store_true", help="only report drift, change nothing")
    args = parser.parse_args(argv)
    version = (args.version or current_version()).removeprefix("v")
    if not _VERSION.match(version):
        raise SystemExit(f"not a release version: {version}")

    pins = lock_pins(LOCK.read_text(encoding="utf-8"))
    files: dict[Path, str] = {}
    names: set[str] = set()
    for path in (PURE, CROSS):
        files[path], pinned = update_pins(path.read_text(encoding="utf-8"), pins, version)
        names |= pinned
    spk = SPK_MAKEFILE.read_text(encoding="utf-8")
    cross = CROSS_MAKEFILE.read_text(encoding="utf-8")
    files[SPK_MAKEFILE] = set_make_var(spk, "SPK_VERS", version)
    files[CROSS_MAKEFILE] = set_make_var(cross, "PKG_VERS", version)

    problems = []
    missing = sorted(set(pins) - names)
    extra = sorted(names - set(pins) - {"homeward"})
    if missing:
        problems.append(f"in the lock file but in neither spksrc file: {', '.join(missing)}")
    if extra:
        problems.append(f"in a spksrc file but not in the lock file: {', '.join(extra)}")
    for path in (PURE, CROSS):
        problems += held_back_problems(files[path], pins)
    changed = [p for p, text in files.items() if p.read_text(encoding="utf-8") != text]
    if args.check:
        problems += [f"{p.relative_to(ROOT).as_posix()} is out of date" for p in changed]
        for problem in problems:
            print(f"spksrc: {problem}", file=sys.stderr)
        if problems:
            print("Run: python tools/spksrc_requirements.py", file=sys.stderr)
        return 1 if problems else 0
    for path in changed:
        path.write_text(files[path], encoding="utf-8", newline="\n")
        print(f"updated {path.relative_to(ROOT).as_posix()}")
    for problem in problems:
        print(f"spksrc: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
