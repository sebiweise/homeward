"""M0 spike: check that pyicloud works on the NAS and measure iCloud Drive behaviour.

Run on the NAS inside a Python 3.12 venv with pyicloud installed. Subcommands:

  login                        sign in (2FA on first run), store a trusted session
  list [PATH]                  list a Drive folder ("" = root)
  tree --out FILE [--compare FILE]
                               walk the whole Drive, time it, store folder etags;
                               with --compare, show which folder etags changed
  roundtrip --folder NAME      upload, list, download, verify and trash a small file
  bigfile FILE --folder NAME   same with a large local file, with timing and peak memory

Throwaway code: not part of the homeward package.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService

STATE_DIR = Path.home() / ".homeward-spike"
CHUNK = 1024 * 1024


def log(**fields: Any) -> None:
    print(json.dumps(fields, default=str), flush=True)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def connect(apple_id: str) -> PyiCloudService:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    password = os.environ.get("HOMEWARD_PASSWORD") or getpass.getpass("Apple ID password: ")
    api = PyiCloudService(apple_id, password, cookie_directory=str(STATE_DIR))
    if api.requires_2fa:
        code = input("2FA code from your Apple device: ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("2FA code rejected")
        if not api.is_trusted_session:
            api.trust_session()
    elif api.requires_2sa:
        sys.exit("Account uses legacy two-step verification; not supported by this spike")
    return api


def node_at(api: PyiCloudService, path: str) -> Any:
    node = api.drive.root
    for part in [p for p in path.split("/") if p]:
        node = node[part]
    return node


def ensure_folder(api: PyiCloudService, name: str) -> Any:
    root = api.drive.root
    if name not in root.dir():
        root.mkdir(name)
        root = api.drive.root
        root.get_children(force=True)
    return root[name]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_login(api: PyiCloudService, _: argparse.Namespace) -> None:
    log(step="login", trusted=api.is_trusted_session)
    try:
        log(step="drive-access", root_items=len(api.drive.root.dir()))
    except Exception as exc:  # ADP or missing Drive shows up here
        log(step="drive-access", error=repr(exc), hint="Advanced Data Protection enabled?")


def cmd_list(api: PyiCloudService, args: argparse.Namespace) -> None:
    for child in node_at(api, args.path).get_children():
        log(name=child.name, type=child.type, size=child.size, modified=child.date_modified)


def walk(node: Any, prefix: str, etags: dict[str, str], counts: dict[str, int]) -> None:
    for child in node.get_children():
        path = f"{prefix}/{child.name}"
        if child.type == "folder":
            counts["folders"] += 1
            etags[path] = child.data.get("etag", "")
            walk(child, path, etags, counts)
        else:
            counts["files"] += 1


def cmd_tree(api: PyiCloudService, args: argparse.Namespace) -> None:
    etags: dict[str, str] = {}
    counts = {"files": 0, "folders": 0}
    start = time.monotonic()
    walk(api.drive.root, "", etags, counts)
    log(step="tree", seconds=round(time.monotonic() - start, 1), **counts)
    Path(args.out).write_text(json.dumps(etags, indent=1))
    if args.compare:
        before = json.loads(Path(args.compare).read_text())
        changed = sorted(p for p in etags if before.get(p) != etags[p])
        removed = sorted(set(before) - set(etags))
        log(step="compare", changed=changed, removed=removed)
    log(step="delta-api-candidates", drive_methods=[m for m in dir(api.drive) if "change" in m])


def upload_download(api: PyiCloudService, local: Path, folder_name: str) -> None:
    folder = ensure_folder(api, folder_name)
    expected = sha256_file(local)
    log(step="upload-start", file=local.name, mb=round(local.stat().st_size / CHUNK, 1))

    start = time.monotonic()
    os.chdir(local.parent)  # pyicloud uses the file object's name as the remote name
    with open(local.name, "rb") as handle:
        folder.upload(handle)
    log(step="upload-done", seconds=round(time.monotonic() - start, 1), peak_rss_mb=peak_rss_mb())

    folder.get_children(force=True)
    remote = folder[local.name]
    log(step="remote-meta", size=remote.size, etag=remote.data.get("etag"))

    target = STATE_DIR / f"download-{local.name}"
    start = time.monotonic()
    digest = hashlib.sha256()
    with remote.open(stream=True) as response, target.open("wb") as out:
        for chunk in response.iter_content(chunk_size=CHUNK):
            digest.update(chunk)
            out.write(chunk)
    log(
        step="download-done",
        seconds=round(time.monotonic() - start, 1),
        peak_rss_mb=peak_rss_mb(),
        sha256_match=digest.hexdigest() == expected,
    )
    target.unlink()
    remote.move_to_trash()
    log(step="cleanup", trashed=local.name)


def cmd_roundtrip(api: PyiCloudService, args: argparse.Namespace) -> None:
    local = STATE_DIR / "homeward-roundtrip.txt"
    local.write_text(f"homeward spike {time.time()}\n")
    upload_download(api, local, args.folder)
    local.unlink()


def cmd_bigfile(api: PyiCloudService, args: argparse.Namespace) -> None:
    upload_download(api, Path(args.file).resolve(), args.folder)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--apple-id", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(func=cmd_login)
    p = sub.add_parser("list")
    p.add_argument("path", nargs="?", default="")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("tree")
    p.add_argument("--out", required=True)
    p.add_argument("--compare")
    p.set_defaults(func=cmd_tree)
    p = sub.add_parser("roundtrip")
    p.add_argument("--folder", required=True)
    p.set_defaults(func=cmd_roundtrip)
    p = sub.add_parser("bigfile")
    p.add_argument("file")
    p.add_argument("--folder", required=True)
    p.set_defaults(func=cmd_bigfile)
    args = parser.parse_args()
    args.func(connect(args.apple_id), args)


if __name__ == "__main__":
    main()
