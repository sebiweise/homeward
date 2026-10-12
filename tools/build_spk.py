#!/usr/bin/env python3
"""Build the Homeward DSM package (.spk) for one architecture.

    python tools/build_spk.py --arch x64       # or: make spk ARCH=x64

The package bundles the Homeward wheel and every dependency as a wheel for the
target CPU (manylinux2014, CPython 3.12). postinst builds a venv from them with
SynoCommunity's Python 3.12, offline. The version comes from the latest git tag
(``vX.Y.Z``): the DSM version is ``X.Y.Z-N`` with N commits since that tag.

Stdlib only; needs git and pip (with network access to PyPI).
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import zlib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MTIME = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
PACKAGE_SRC = ROOT / "package"

PACKAGE_NAME = "homeward"
DISPLAY_NAME = "Homeward"
OS_MIN_VER = "7.2-64570"
PYTHON_VERSION = "3.12"
DEPENDENCIES = "python312"

# DSM platform names per architecture, as used by SynoCommunity's DSM 7.2
# toolchains (and therefore by the python312 package Homeward depends on).
ARCHS: dict[str, tuple[str, str]] = {
    "x64": (
        "x86_64",
        "apollolake avoton braswell broadwell broadwellnk broadwellnkv2 broadwellntbap "
        "bromolow denverton epyc7002 epyc7003 epyc7003ntb geminilake geminilakenk grantley "
        "icelaked kvmx64 purley r1000 r1000nk v1000 v1000nk",
    ),
    "aarch64": ("aarch64", "rtd1296 rtd1619b armada37xx"),
}

LIFECYCLE = {
    "preinst": "service_preinst",
    "postinst": "service_postinst",
    "preuninst": "service_preuninst",
    "postuninst": "service_postuninst",
    "preupgrade": "service_preupgrade",
    "postupgrade": "service_postupgrade",
    "start-stop-status": 'service_start_stop_status "$1"',
}

DESCRIPTION = (
    "Backs up iCloud Photos to the NAS and syncs iCloud Drive folders "
    "(upload, download or bidirectional)."
)
DESCRIPTION_GER = (
    "Sichert iCloud-Fotos auf der NAS und synchronisiert iCloud-Drive-Ordner "
    "(Upload, Download oder bidirektional)."
)


def run(*cmd: str, cwd: Path = ROOT) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout


def dsm_version() -> str:
    """``X.Y.Z-N`` from ``git describe``; ``0.0.0-N`` (N = all commits) without a tag."""
    try:
        described = run("git", "describe", "--tags", "--long", "--match", "v[0-9]*").strip()
    except subprocess.CalledProcessError:
        return f"0.0.0-{run('git', 'rev-list', '--count', 'HEAD').strip()}"
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)-(\d+)-g[0-9a-f]+", described)
    if not match:
        raise SystemExit(f"tag {described!r} is not of the form vX.Y.Z")
    return f"{match.group(1)}-{match.group(2)}"


def build_wheelhouse(arch: str, wheelhouse: Path) -> list[Path]:
    machine = ARCHS[arch][0]
    wheelhouse.mkdir(parents=True)
    run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), ".")
    (homeward,) = wheelhouse.glob("homeward-*.whl")
    run(
        sys.executable, "-m", "pip", "download",
        "--dest", str(wheelhouse),
        "--only-binary=:all:",
        "--platform", f"manylinux2014_{machine}",
        "--platform", f"manylinux_2_17_{machine}",
        "--python-version", PYTHON_VERSION,
        "--implementation", "cp",
        "--abi", "cp" + PYTHON_VERSION.replace(".", ""),
        f"{homeward}[icloud]",
    )  # fmt: skip
    wheels = sorted(wheelhouse.iterdir())
    for wheel in wheels:
        check_wheel(wheel, machine)
    return wheels


def check_wheel(wheel: Path, machine: str) -> None:
    """Fail on anything that would not install on the target CPU."""
    if wheel.suffix != ".whl":
        raise SystemExit(f"{wheel.name}: only wheels can be bundled")
    platform_tag = wheel.stem.split("-")[-1]
    if platform_tag != "any" and not all(p.endswith(machine) for p in platform_tag.split(".")):
        raise SystemExit(f"{wheel.name}: wrong platform for {machine}")


def png(size: int, pixel: Callable[[float, float], tuple[int, int, int, int]]) -> bytes:
    """Encode an RGBA PNG; ``pixel`` gets coordinates in 0..1."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            # 2x2 supersampling for smooth edges.
            offsets = ((0.25, 0.25), (0.25, 0.75), (0.75, 0.25), (0.75, 0.75))
            samples = [pixel((x + dx) / size, (y + dy) / size) for dx, dy in offsets]
            rows.extend(sum(s[i] for s in samples) // 4 for i in range(4))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def default_icon(x: float, y: float) -> tuple[int, int, int, int]:
    """A white house on a rounded blue square, used until package/icons has real icons."""
    radius = 0.18
    cx, cy = min(max(x, radius), 1 - radius), min(max(y, radius), 1 - radius)
    if (x - cx) ** 2 + (y - cy) ** 2 > radius**2:
        return (0, 0, 0, 0)
    white = (255, 255, 255, 255)
    roof = 0.22 <= y <= 0.5 and abs(x - 0.5) <= (y - 0.22) / 0.28 * 0.3
    body = 0.5 <= y <= 0.78 and 0.29 <= x <= 0.71
    door = 0.6 <= y <= 0.78 and 0.45 <= x <= 0.55
    if (roof or body) and not door:
        return white
    return (int(40 + 40 * y), int(110 + 50 * y), 220, 255)


def icon(size: int) -> bytes:
    custom = PACKAGE_SRC / "icons" / f"icon_{size}.png"
    return custom.read_bytes() if custom.exists() else png(size, default_icon)


def info(version: str, arch: str, checksum: str, has_ui: bool) -> str:
    fields = {
        "package": PACKAGE_NAME,
        "version": version,
        "os_min_ver": OS_MIN_VER,
        "displayname": DISPLAY_NAME,
        "description": DESCRIPTION,
        "description_ger": DESCRIPTION_GER,
        "arch": ARCHS[arch][1],
        "maintainer": "Sebastian Goscinski",
        "maintainer_url": "https://github.com/sebiweise/homeward",
        "support_url": "https://github.com/sebiweise/homeward/issues",
        "install_dep_packages": DEPENDENCIES,
        "thirdparty": "yes",
        "beta": "yes",
        "checksum": checksum,
    }
    if has_ui:  # the DSM app (M6) lives in package/ui
        fields["dsmuidir"] = "ui"
        fields["dsmappname"] = "SYNO.SDS.Homeward.Instance"
    return "".join(f'{key}="{value}"\n' for key, value in fields.items())


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = mode
    entry.mtime = MTIME
    tar.addfile(entry, io.BytesIO(data))


def add_tree(tar: tarfile.TarFile, src: Path, prefix: str) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            add_bytes(tar, f"{prefix}/{path.relative_to(src).as_posix()}", path.read_bytes(), mode)


def build(arch: str, out_dir: Path, work: Path) -> Path:
    version = dsm_version()
    if work.exists():
        shutil.rmtree(work)
    wheels = build_wheelhouse(arch, work / "wheelhouse")

    # package.tgz is extracted to /var/packages/homeward/target.
    ui = PACKAGE_SRC / "ui"
    has_ui = (ui / "config").exists()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tgz:
        for wheel in wheels:
            add_bytes(tgz, f"wheelhouse/{wheel.name}", wheel.read_bytes())
        add_bytes(tgz, "LICENSE", (ROOT / "LICENSE").read_bytes())
        if has_ui:
            add_tree(tgz, ui, "ui")
    package_tgz = buf.getvalue()

    out_dir.mkdir(parents=True, exist_ok=True)
    spk = out_dir / f"{PACKAGE_NAME}-{version}-{arch}.spk"
    checksum = hashlib.md5(package_tgz).hexdigest()
    with tarfile.open(spk, mode="w", format=tarfile.GNU_FORMAT) as tar:
        add_bytes(tar, "INFO", info(version, arch, checksum, has_ui).encode())
        add_bytes(tar, "package.tgz", package_tgz)
        add_bytes(tar, "PACKAGE_ICON.PNG", icon(72))
        add_bytes(tar, "PACKAGE_ICON_256.PNG", icon(256))
        add_bytes(tar, "LICENSE", (ROOT / "LICENSE").read_bytes())
        for name in ("privilege", "resource"):
            add_bytes(tar, f"conf/{name}", (PACKAGE_SRC / "conf" / name).read_bytes())
        setup = PACKAGE_SRC / "scripts" / "service-setup.sh"
        add_bytes(tar, "scripts/service-setup.sh", setup.read_bytes(), 0o755)
        for script, call in LIFECYCLE.items():
            body = f'#!/bin/sh\n. "$(dirname "$0")/service-setup.sh"\n{call}\n'
            add_bytes(tar, f"scripts/{script}", body.encode(), 0o755)
    print(f"built {spk} ({spk.stat().st_size // 1024} KiB, {len(wheels)} wheels)")
    return spk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arch", choices=sorted(ARCHS), required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--work", type=Path, help="scratch directory (default: build/spk-ARCH)")
    parser.add_argument("--print-version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    if args.print_version:
        print(dsm_version())
        return 0
    build(args.arch, args.out, args.work or ROOT / "build" / f"spk-{args.arch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
