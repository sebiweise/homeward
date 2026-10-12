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
  off (`trust_folder_etags=False`) until the probe in ADR 0002 confirms that deep
  changes change every ancestor's etag.
- A file's version is `docwsid:etag`. Only plain files and folders sync; app
  libraries (Shortcuts, Scriptable) and other node types such as packages are skipped.

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
trash_days = 30                   # NAS trash retention, 0 = keep forever
```

Architecture decisions are recorded in [`adr/`](adr/).
