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
- NAS deletions go to `.homeward-papierkorb/<date>/` below the pair's folder.

## Configuration

```toml
state_dir = "/var/packages/homeward/var/state"

[[pair]]
name = "docs"
account = "sebastian"
local = "/volume1/homes/sebastian/Documents"
remote = "Documents"
mode = "bidirectional"            # upload | download | bidirectional
remote_provider = "icloud_drive"  # "folder" syncs with a second NAS folder (testing)
exclude = ["*.tmp"]
max_delete_ratio = 0.10
max_delete_count = 50
```

## Photos backup

Photos only go iCloud → NAS (`core/photos.py`, adapter `providers/icloud_photos.py`).
One `[[photos]]` job backs up one Apple ID into one NAS folder; several jobs give
several Apple IDs, each with its own login session and state file
(`<state_dir>/<account>.photos.sqlite3`).

```
<target>/2024/05/IMG_0001.HEIC            original, Year/Month by capture date (NAS time zone)
<target>/2024/05/IMG_0001.MOV             video part of a Live Photo
<target>/Alben/Urlaub/IMG_0001.HEIC       hard link to the file above (no extra space)
<target>/Geteilte Mediathek/2024/05/...   the iCloud Shared Library
```

- **Incremental**: the libraries' CloudKit sync tokens form a cursor. If it did not change
  since the last complete run and every file is still there, nothing is listed at all.
  Otherwise known photos are skipped by key, only new ones are downloaded.
- **Never deletes**: a photo deleted in iCloud stays on the NAS (marked `gone` in the
  state). A changed original moves the old file to `.homeward-papierkorb/<date>/` first.
  When a photo leaves an album only its album link is removed; an album file that is no
  longer the same file as the original goes to the trash instead.
- Same name, different photo: `IMG_0001 (2).JPG`; existing NAS files are never overwritten.
- Downloads stream into a temp file, are checked against the size iCloud reports and are
  hard-linked into place, so a crash leaves no partial photo.
- Shared Library albums are not readable through pyicloud 2.7, so only the personal
  library's albums get album folders.

```toml
[[photos]]
name = "sebastian"
account = "sebastian"                # session + state file name
apple_id = "sebastian@example.com"
target = "/volume1/photo/iCloud Sebastian"
libraries = ["personal", "shared"]   # default: both
folder_scheme = "{year}/{month}"     # {year}, {month}, {day}
albums = true                        # album folders with hard links
videos = true
live_videos = true                   # the video part of Live Photos
shared_folder = "Geteilte Mediathek"
albums_folder = "Alben"
```

Login: `homeward login --account sebastian` asks for the password and the 2FA code once
and keeps the trusted session in `<state_dir>/sessions/<account>/` (mode 0700).
`homeward photos [--job NAME] [--dry-run]` runs the backup and never prompts; an expired
session ends the job with a hint to log in again.

Architecture decisions are recorded in [`adr/`](adr/).
