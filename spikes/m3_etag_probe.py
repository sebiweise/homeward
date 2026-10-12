"""M3 probe: check iCloud Drive etag behaviour and Homeward's streamed upload on the NAS.

Run in the M0 venv with the homeward wheel installed. It reuses the trusted session
from the M0 spike (``~/.homeward-spike``), so no new 2FA code is needed. Subcommands:

  probe [--bigfile FILE]   create a test folder four levels deep, change files in the
                           deepest folder and report which ancestor etags changed;
                           checks file etags stay stable after an upload, rename and
                           move through Homeward's provider; with --bigfile also
                           uploads FILE with the streamed upload and reports peak RAM.
                           Everything goes to the iCloud trash at the end.
  snapshot --out F [PATH]  store the etags of all folders below PATH ("" = whole Drive)
  compare F [PATH]         show which folder etags changed since the snapshot

Throwaway code: not part of the homeward package.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService

from homeward.providers.drive_api import ROOT_ID, PyiCloudDriveApi, item_name
from homeward.providers.icloud_drive import ICloudDriveProvider

STATE_DIR = Path.home() / ".homeward-spike"
PROBE = "homeward-etag-probe"
LEVELS = ("L1", "L2", "L3")


def log(**fields: Any) -> None:
    print(json.dumps(fields, default=str), flush=True)


def peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def connect(apple_id: str) -> PyiCloudService:
    password = os.environ.get("HOMEWARD_PASSWORD") or getpass.getpass("Apple ID password: ")
    api = PyiCloudService(apple_id, password, cookie_directory=str(STATE_DIR))
    if api.requires_2fa:
        code = input("2FA code from your Apple device: ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("2FA code rejected")
        if not api.is_trusted_session:
            api.trust_session()
    return api


def folder_etags(api: PyiCloudDriveApi, chain: list[str]) -> dict[str, str]:
    """Etag of each folder in ``chain`` as its parent lists it."""
    result = {"(drive root)": api.folders([ROOT_ID])[0]["etag"]}
    folder = api.folders([ROOT_ID])[0]
    path = ""
    for name in chain:
        child = next(i for i in folder["items"] if item_name(i) == name)
        path = f"{path}/{name}"
        result[path] = child["etag"]
        folder = api.folders([child["drivewsid"]])[0]
    return result


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [path for path in before if before[path] != after.get(path)]


def step(api: PyiCloudDriveApi, chain: list[str], name: str, action: Any) -> dict[str, Any]:
    before = folder_etags(api, chain)
    try:
        action()
    except Exception as exc:  # report and keep probing
        log(step=name, error=repr(exc))
        return {"step": name, "changed": [], "error": repr(exc)}
    time.sleep(3)
    after = folder_etags(api, chain)
    result = {"step": name, "changed": changed(before, after)}
    log(**result)
    return result


def cmd_probe(service: PyiCloudService, args: argparse.Namespace) -> None:
    api = PyiCloudDriveApi(service.drive)
    chain = [PROBE, *LEVELS]
    deep = "/".join(LEVELS)
    provider = ICloudDriveProvider(api, PROBE)
    provider.write(f"{deep}/seed.txt", io.BytesIO(b"seed"), time.time())
    provider.write("top.txt", io.BytesIO(b"top"), time.time())
    log(step="setup", folders=chain)

    results = [
        step(api, chain, "new file in L3", lambda: provider.write(
            f"{deep}/new.txt", io.BytesIO(b"new"), time.time())),
        step(api, chain, "rename file in L3", lambda: provider.move(
            f"{deep}/new.txt", f"{deep}/renamed.md")),
        step(api, chain, "replace file in L3 (trash + upload)", lambda: (
            provider.delete(f"{deep}/seed.txt"),
            provider.write(f"{deep}/seed.txt", io.BytesIO(b"seed v2"), time.time()))),
        step(api, chain, "trash file in L3", lambda: provider.delete(f"{deep}/renamed.md")),
    ]  # fmt: skip

    # File etags: what Homeward records after an upload must match later listings.
    meta = provider.write(f"{deep}/stable.txt", io.BytesIO(b"stable"), time.time())
    time.sleep(30)
    later = ICloudDriveProvider(api, PROBE).list_files()
    log(step="file version after upload", recorded=meta.version,
        listed_30s_later=later[f"{deep}/stable.txt"].version,
        stable=later[f"{deep}/stable.txt"].version == meta.version,
        size_ok=later[f"{deep}/stable.txt"].size == 6)  # fmt: skip
    # For bidirectional rename detection (M4): does a file keep docwsid/etag when moved?
    moved = provider.move(f"{deep}/stable.txt", "L1/moved.txt")
    log(step="move across folders", ok="L1/moved.txt" in provider.list_files(),
        before=meta.version, after=moved.version,
        same_docwsid=moved.version.split(":")[0] == meta.version.split(":")[0],
        same_version=moved.version == meta.version)  # fmt: skip
    renamed = provider.move("L1/moved.txt", "L1/renamed.txt")
    log(step="rename in place", before=moved.version, after=renamed.version,
        same_version=renamed.version == moved.version)  # fmt: skip

    if args.bigfile:
        big = Path(args.bigfile)
        expected = sha256_file(big)
        start = time.monotonic()
        with big.open("rb") as handle:
            provider.write(big.name, handle, big.stat().st_mtime)
        log(step="bigfile upload", mb=round(big.stat().st_size / 2**20, 1),
            seconds=round(time.monotonic() - start, 1), peak_rss_mb=peak_rss_mb())  # fmt: skip
        log(step="bigfile check", sha256_match=provider.sha256(big.name) == expected,
            peak_rss_mb=peak_rss_mb())  # fmt: skip

    deep_changes_reach_root = all(
        set(r["changed"]) >= {f"/{PROBE}", f"/{PROBE}/L1", f"/{PROBE}/L1/L2"}
        for r in results
        if "error" not in r
    )
    log(step="verdict", deep_changes_change_all_ancestor_etags=deep_changes_reach_root)

    root = api.folders([ROOT_ID])[0]
    probe = next(i for i in root["items"] if item_name(i) == PROBE)
    api.trash(probe["drivewsid"], probe["etag"])
    log(step="cleanup", trashed=PROBE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(2**20):
            digest.update(chunk)
    return digest.hexdigest()


def walk_etags(api: PyiCloudDriveApi, path: str) -> dict[str, str]:
    folder = api.folders([ROOT_ID])[0]
    for name in [p for p in path.split("/") if p]:
        child = next(i for i in folder["items"] if item_name(i) == name)
        folder = api.folders([child["drivewsid"]])[0]
    etags: dict[str, str] = {}
    level = [("", folder)]
    while level:
        subfolders = [
            (f"{prefix}/{item_name(i)}", i)
            for prefix, f in level
            for i in f["items"]
            if i.get("type") == "FOLDER"
        ]
        for sub_path, item in subfolders:
            etags[sub_path] = item["etag"]
        for prefix, f in level:  # files too: does an edit change the file's etag?
            for i in f["items"]:
                if i.get("type") == "FILE":
                    etags[f"{prefix}/{item_name(i)}"] = (
                        f"{i['docwsid']}:{i['etag']} size={i.get('size')} "
                        f"modified={i.get('dateModified')}"
                    )
        fetched = []
        for start in range(0, len(subfolders), 50):
            batch = subfolders[start : start + 50]
            fetched += api.folders([i["drivewsid"] for _, i in batch])
        level = [(p, f) for (p, _), f in zip(subfolders, fetched, strict=True)]
    return etags


def cmd_snapshot(service: PyiCloudService, args: argparse.Namespace) -> None:
    start = time.monotonic()
    etags = walk_etags(PyiCloudDriveApi(service.drive), args.path)
    Path(args.out).write_text(json.dumps(etags, indent=1))
    log(step="snapshot", entries=len(etags), seconds=round(time.monotonic() - start, 1))


def cmd_compare(service: PyiCloudService, args: argparse.Namespace) -> None:
    before = json.loads(Path(args.file).read_text())
    after = walk_etags(PyiCloudDriveApi(service.drive), args.path)
    for path in changed(before, after):
        log(step="changed", path=path, before=before[path], after=after.get(path))
    log(step="compare", changed=changed(before, after),
        new=sorted(set(after) - set(before)), removed=sorted(set(before) - set(after)))  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--apple-id", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--bigfile")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("snapshot")
    p.add_argument("--out", required=True)
    p.add_argument("path", nargs="?", default="")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("compare")
    p.add_argument("file")
    p.add_argument("path", nargs="?", default="")
    p.set_defaults(func=cmd_compare)
    args = parser.parse_args()
    args.func(connect(args.apple_id), args)


if __name__ == "__main__":
    main()
