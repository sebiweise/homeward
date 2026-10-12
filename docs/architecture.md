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

```toml
[daemon]
interval_minutes = 60             # each pair runs once per interval (minimum 5)
```

## Package

The `.spk` (built by `tools/build_spk.py`) installs into `/var/packages/homeward`:
the venv in `target/env`, config, state, `status.json` and the log in `var/`. The
daemon `homeward-daemon` runs as the unprivileged package user and starts one
`homeward sync --pair` process per due pair. Details in
[ADR 0002](adr/0002-dsm-package.md).

Architecture decisions are recorded in [`adr/`](adr/).
