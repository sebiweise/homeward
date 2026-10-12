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
  (it survives a rename), the same iCloud docwsid (via the provider's `file_identity`;
  iCloud changes the etag on a move) with the same size, or the same known checksum. Such deletes are marked as renames
  and do not count for the delete brake, so renaming a folder does not stop the run.
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
account = "sebastian"
apple_id = "sebastian@example.com"  # optional if account is the Apple ID
local = "/volume1/homes/sebastian/Documents"
remote = "Documents"
mode = "bidirectional"            # upload | download | bidirectional
beta = true                       # required for bidirectional while it is in beta
remote_provider = "icloud_drive"  # "folder" syncs with a second NAS folder (testing)
exclude = ["*.tmp"]
max_delete_ratio = 0.10
max_delete_count = 50
trash_days = 30                   # NAS trash retention, 0 = keep forever
```

Architecture decisions are recorded in [`adr/`](adr/).
