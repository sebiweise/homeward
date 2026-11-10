#!/usr/bin/env python3
"""Build the Homeward DSM package (.spk) for one architecture.

    python tools/build_spk.py --arch x64       # or: make spk ARCH=x64

The package bundles the Homeward wheel and every dependency as a wheel for the
target CPU (manylinux2014, CPython 3.12). postinst builds a venv from them with
SynoCommunity's Python 3.12, offline. The version comes from the latest git tag
(``vX.Y.Z``): the DSM version is ``X.Y.Z-N`` with N commits since that tag.

The dependencies are the versions pinned, with hashes, in requirements/icloud.txt.

Stdlib only; needs git and pip (with network access to PyPI).
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MTIME = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
PACKAGE_SRC = ROOT / "package"
LOCK_FILE = ROOT / "requirements" / "icloud.txt"

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
    # Exactly the pinned versions, and only files whose hash is in the lock file.
    run(
        sys.executable, "-m", "pip", "download",
        "--dest", str(wheelhouse),
        "--only-binary=:all:",
        "--require-hashes", "--no-deps",
        "--platform", f"manylinux2014_{machine}",
        "--platform", f"manylinux_2_17_{machine}",
        "--python-version", PYTHON_VERSION,
        "--implementation", "cp",
        "--abi", "cp" + PYTHON_VERSION.replace(".", ""),
        "--requirement", str(LOCK_FILE),
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


def icon(size: int) -> bytes:
    """The package icon: package/icons if it has one, else the app's own icon
    (package/ui/images, drawn by tools/make_icons.py)."""
    for folder in (PACKAGE_SRC / "icons", PACKAGE_SRC / "ui" / "images"):
        candidate = folder / f"icon_{size}.png"
        if candidate.exists():
            return candidate.read_bytes()
    raise SystemExit(
        f"build_spk: no icon_{size}.png; run python tools/make_icons.py package/ui/images"
    )


def info(version: str, arch: str, checksum: str, has_ui: bool) -> str:
    fields = {
        "package": PACKAGE_NAME,
        "version": version,
        "os_min_ver": OS_MIN_VER,
        "displayname": DISPLAY_NAME,
        "description": DESCRIPTION,
        "description_ger": DESCRIPTION_GER,
        "arch": ARCHS[arch][1],
        "maintainer": "sebiweise",
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

    # Before the .spk is opened for writing: a missing icon must not leave a broken
    # package in place of one built before.
    icons = {"PACKAGE_ICON.PNG": icon(72), "PACKAGE_ICON_256.PNG": icon(256)}
    out_dir.mkdir(parents=True, exist_ok=True)
    spk = out_dir / f"{PACKAGE_NAME}-{version}-{arch}.spk"
    # DSM's INFO file wants an MD5 of package.tgz; it is an integrity check, not security.
    checksum = hashlib.md5(package_tgz, usedforsecurity=False).hexdigest()
    with tarfile.open(spk, mode="w", format=tarfile.GNU_FORMAT) as tar:
        add_bytes(tar, "INFO", info(version, arch, checksum, has_ui).encode())
        add_bytes(tar, "package.tgz", package_tgz)
        for name, data in icons.items():
            add_bytes(tar, name, data)
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


def _inside_repo(path: Path) -> Path:
    """Build output and scratch space stay inside the checkout: ``--work`` is deleted
    before every build, so a typo must not point it at anything else."""
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise SystemExit(f"build_spk: {path} is outside the repository ({ROOT})")
    return resolved


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arch", choices=sorted(ARCHS), required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--work", type=Path, help="scratch directory (default: build/spk-ARCH)")
    parser.add_argument("--print-version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    if args.print_version:
        print(dsm_version())
        return
    out = _inside_repo(args.out)
    work = _inside_repo(args.work or ROOT / "build" / f"spk-{args.arch}")
    build(args.arch, out, work)


if __name__ == "__main__":
    main()
