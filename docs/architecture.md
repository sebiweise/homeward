# Architecture

Homeward separates a pure Python sync core from everything that touches DSM or Apple,
so the core can be tested without a NAS and without an Apple account.

```
UI (DSM, Ext.js)   Daemon (scheduler)   CLI
        \               |              /
               core: planner, executor, rules, state (SQLite)
                         |
        providers: icloud_photos, icloud_drive (pyicloud) | nas | fake
```

- `core/` never imports `pyicloud` or DSM code; this is enforced by
  `tests/unit/test_core_isolation.py`.
- Photos only sync iCloud → NAS. Drive supports `upload`, `download` and `bidirectional`.
- Bidirectional Drive sync compares three states per file (NAS now, iCloud now, last
  common state from the state DB) and never deletes or overwrites without a way back.

## Sync core

One run of a pair: both providers list their files, `core/planner.py` compares each
path with its last common state from the state DB (`core/state.py`, one SQLite file
per account, WAL) and looks up the decision in `core/rules.py`. `core/executor.py`
then applies the plan and records every finished step, so a failed step is simply
planned again next run.

Per side a file is `unchanged`, `changed` or `deleted` (it has a state record) or
`new` / `absent` (it has none). Bidirectional table:

| NAS | iCloud | Action |
| --- | --- | --- |
| changed | unchanged | upload |
| unchanged | changed | download |
| changed | changed | same content: record, else conflict (keep both) |
| deleted | unchanged | delete in iCloud (iCloud trash) |
| unchanged | deleted | move to the NAS trash |
| deleted | changed | download (changed version wins) |
| changed | deleted | upload (changed version wins) |
| deleted | deleted | forget |
| new | absent | upload |
| absent | new | download |
| new | new | same content: record, else conflict (keep both) |

A conflict renames the NAS file to `Name (Konflikt NAS 2026-10-11).ext`, downloads the
iCloud version to the original name and uploads the conflict copy. In `upload` and
`download` mode the source side wins and files that only exist on the target are left
alone. Any overwrite moves the old version to the trash first. Every row of all three
tables has a scenario in `tests/scenarios/test_decision_table.py`.

Safety nets:

- **First run** of a pair (no state yet) never deletes or overwrites.
- **Delete brake**: a run that would delete more than 10 % of the tracked files or more
  than 50 files stops until approved (`--allow-deletes`). Both limits are per pair.
- **Dry run**: `homeward sync --dry-run` prints the plan and changes nothing.
- **Exclusions**: Synology folders, `.DS_Store`, the trash and temp files, plus patterns
  per pair.
- NAS deletions go to the share's Synology recycle bin (`#recycle`) when it is enabled,
  so DSM's own retention applies; otherwise, or if Homeward may not write there, to
  `.homeward-papierkorb/<date>/` below the pair's folder. Day folders there older than
  `trash_days` (default 30, 0 keeps them forever) are removed after each run.
- iCloud deletions go to iCloud's "Recently Deleted" (kept 30 days by Apple).
- **Deletes run last**: every run transfers first and deletes afterwards.

### Bidirectional (beta)

`mode = "bidirectional"` needs `beta = true` in the pair until it has proven itself
on real folders.

- **Renames and moves** are carried out as delete plus new file: the old name goes to
  the other side's trash, the new one is transferred. The planner pairs a delete with
  a new file on the same side when the content matches: the same NAS version token
  (it survives a rename), the same iCloud docwsid with the same size (via the
  provider's `file_identity`; iCloud keeps the docwsid but changes the etag on a
  rename or move, see ADR 0002), or the same known checksum. Such deletes are marked
  as renames and do not count for the delete brake, so renaming a folder does not
  stop the run.
  If the new file fails to transfer, the old one is kept until a later run.
- **Upper/lower case**: iCloud Drive does not keep `a.txt` and `A.txt` apart. A
  provider with `case_insensitive = True` makes the planner skip uploads whose path
  (or a parent folder) differs only in case from one already in iCloud; the file stays
  on the NAS and shows up as skipped in every run. A case-only rename works: its
  delete runs before the upload so the name is free.
- **Conflicts** keep both versions (see above). A second conflict on the same file and
  day is numbered: `Name (Konflikt NAS 2026-10-11 2).ext`.
- `tests/scenarios/test_bidirectional.py` covers these cases and simulates a week of
  hourly runs with random edits, renames and deletes on both sides, checking after
  every run that no content was lost, both sides match and a second run has nothing
  left to do.

## iCloud Drive provider

`providers/icloud_drive.py` maps one Drive folder to the provider interface on top of
`providers/drive_api.py`, a thin seam over pyicloud's authenticated session:

- **Uploads are streamed** (`providers/multipart.py`): the request body is read from
  disk while it is sent, with a known `Content-Length`. pyicloud's own upload builds
  the whole body in memory (~2x the file size, ADR 0001). A file only appears in its
  folder after the final commit request, so a failed upload leaves nothing behind.
- **Listing** walks the tree level by level and fetches up to 50 folders per request.
  A folder whose etag is unchanged since the last run can be served from a cache
  (`<state_dir>/<account>.drive-folders.json`) including its whole subtree; this is
  off (`trust_folder_etags=False`): on a real account a rename changes no folder
  etag (ADR 0002), so a cached listing would miss it.
- A file's version is `docwsid:etag`. Only plain files and folders sync; app
  libraries (Shortcuts, Scriptable) and other node types such as packages are skipped.

## Configuration

```toml
state_dir = "/var/packages/homeward/var/state"

[[pair]]
name = "docs"
account = "alice"
apple_id = "alice@example.com"  # optional if account is the Apple ID
local = "/volume1/homes/alice/Documents"
remote = "Documents"
mode = "bidirectional"            # upload | download | bidirectional
beta = true                       # required for bidirectional while it is in beta
remote_provider = "icloud_drive"  # "folder" syncs with a second NAS folder (testing)
exclude = ["*.tmp"]
max_delete_ratio = 0.10
max_delete_count = 50
trash_days = 30                   # NAS trash retention, 0 = keep forever
```

## Photos backup

Photos only go iCloud → NAS (`core/photos/`: `model`, `plan`, `executor`, `integrity`;
adapter `providers/icloud_photos.py`, with every pyicloud internal it relies on in
`providers/pyicloud_compat.py` and covered by contract tests).
One `[[photos]]` job backs up one Apple ID into one NAS folder; several jobs give
several Apple IDs, each with its own login session and state file
(`<state_dir>/<account>.photos.sqlite3`).

```
<target>/2024/05/IMG_0001.HEIC                original, Year/Month by capture date (NAS time zone)
<target>/2024/05/IMG_0001.MOV                 video part of a Live Photo
<target>/2024/05/IMG_0001.JPG                 second file of a RAW+JPEG shot
<target>/2024/05/IMG_0001 (bearbeitet).JPG    edit made in the Photos app, full size
<target>/2024/05/IMG_0001.HEIC.xmp            caption, keywords, location, favorite
<target>/Alben/Urlaub/IMG_0001.HEIC           hard link to the original (no extra space)
<target>/Favoriten/IMG_0001.HEIC              hard link, too
<target>/Geteilte Mediathek/2024/05/...       the iCloud Shared Library
<target>/Geteilte Alben/Hochzeit/...          the older shared albums (no Year/Month)
<target>/Ausgeblendet/2024/05/...             hidden photos (off by default)
```

- **Incremental**: the libraries' CloudKit sync tokens form a cursor. If it did not change
  since the last complete run and every file is still there, nothing is listed at all.
  Otherwise known photos are skipped by key, only new ones are downloaded.
- **Never deletes** by default: a photo deleted in iCloud stays on the NAS (marked `gone`
  in the state). With `mirror_deletes` a full listing with the same selection (libraries,
  file types) as the last complete run moves it and its album links to the trash
  instead, behind a delete brake (held deletions keep neither cursor nor full-listing
  time, so the approving run reads everything again). `--remove-gone` deletes the photos
  marked `gone` the same way, without the brake (an explicit user action). Photo trash
  is the share's `#recycle` when enabled, else `.homeward-papierkorb/`. A changed original moves the old file to `.homeward-papierkorb/<date>/` first.
  When a photo leaves an album only its album link is removed; an album file that is no
  longer the same file as the original goes to the trash instead.
- **Library moves**: a photo that disappears from one library while one with the same
  resource and checksum appears in the other is moved on the NAS (`MOVE`), not
  downloaded again.
- **Weekly integrity check** (`integrity.py`): every recorded file is compared with its
  downloaded size; a damaged one goes to the trash with its album links (same inode
  only) so the plan restores it. Photos iCloud no longer has are only reported.
- Same name, different photo: `IMG_0001 (2).JPG`; existing NAS files are never overwritten.
- Downloads stream into a temp file, are checked against the size iCloud reports and are
  renamed into place without replacing (`RENAME_NOREPLACE`, so DSM's indexer sees them),
  so a crash leaves no partial photo.
- Album folders come from the personal library's albums, which can also hold Shared
  Library photos. The Shared Library's own albums are not readable through pyicloud 2.7.
- Not possible with pyicloud 2.7: people/faces and memories.

```toml
[[photos]]
name = "alice"
account = "alice"                # session + state file name
apple_id = "alice@example.com"
target = "/volume1/photo/iCloud Alice"
libraries = ["personal", "shared", "shared_albums"]   # default: all three
folder_scheme = "{year}/{month}"     # {year}, {month}, {day}
albums = true                        # album folders with hard links
favorites = true                     # favorites folder with hard links
videos = true
live_videos = true                   # the video part of Live Photos
raw_alternative = true               # both files of RAW+JPEG shots
edited = true                        # "Name (bearbeitet).ext" next to the original
xmp = true                           # sidecars, only for photos with such metadata
hidden = false                       # hidden photos, below hidden_folder
workers = 4                          # parallel downloads, 1 to 8
shared_folder = "Geteilte Mediathek"
albums_folder = "Alben"
favorites_folder = "Favoriten"
hidden_folder = "Ausgeblendet"
shared_albums_folder = "Geteilte Alben"
source_folders = true                # false: no shared_folder / shared_albums_folder level
```

Every switch is meant to become an option in the DSM UI (M6). Switching a feature off
later keeps the files it already produced (their keys are marked `gone`).

Login: `homeward login --account alice` asks for the password and the 2FA code once
and keeps the trusted session in `<state_dir>/sessions/<account>/` (mode 0700).
`homeward photos [--job NAME] [--dry-run]` runs the backup and never prompts; an expired
session ends the job with a hint to log in again.

## Package and daemon

```toml
[daemon]
interval_minutes = 60             # each pair and photos job runs once per interval (5 to 10080)
```

A `[[pair]]` or `[[photos]]` table may set its own `interval_minutes`; the DSM UI edits
both (`settings.get`/`settings.update` for the global value).

The `.spk` (built by `tools/build_spk.py`) installs into `/var/packages/homeward`:
the venv in `target/env`, config, state, `status.json` and the log in `var/`. The
daemon `homeward-daemon` runs as the unprivileged package user and starts one
`homeward sync --pair` or `homeward photos --job` process per due job. Details in
[ADR 0003](adr/0003-dsm-package.md).

Jobs run in lanes: all jobs of one Apple ID share the lane `account:<name>` (one
session, one at a time), a job without iCloud is its own lane, and at most
`MAX_PARALLEL_RUNS` (2) lanes run at once. `status.json` lists them in `running_jobs`.
A job's `window` delays its next due time to the window's start, and the daemon stops a
scheduled run (not a "sync now") when its window closes (result `window_closed`, due
again at the next opening). `max_rate_kb` wraps every transfer stream in a shared
token-bucket `Throttle` (`core/limits.py`); `tell`/`seek` pass through so uploads keep
their length. The scheduler also keeps each job's `last_success` and warns once
(`not_backed_up`) after `STALE_DAYS` (3) without one.

Exit codes of the CLI: 0 ok, 1 failed, 2 config error, 3 delete brake, 4 waiting for
iCloud, 5 stopped, 6 sign-in needed (one DSM notification per Apple ID). The daemon
writes its own log and the runs' output through a rotating handler (5 MB, 3 backups).

Architecture decisions are recorded in [`adr/`](adr/).
