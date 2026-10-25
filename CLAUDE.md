# Homeward

Native Synology DSM 7.2 package that syncs iCloud Photos (backup) and
iCloud Drive (upload / download / bidirectional) with the NAS.

## Principles
- Never lose data: no overwrite or delete without a recoverable copy.
- Core (`src/homeward/core`) must not import DSM or pyicloud code.
- All iCloud access goes through `providers/` adapters.
- Every rule in the bidirectional decision table has a test.

## Commands
- `make test`   pytest with the fake provider
- `make lint`   ruff + mypy
- `make spk ARCH=x64`   build the package

## Conventions
- Python 3.12, type hints, English code and comments.
- UI strings in `package/ui/texts/{enu,ger}/strings`.
- Version comes from the git tag only.

## Architecture map
Details in `docs/architecture.md` and `docs/adr/`; read those before reading code.
- `src/homeward/core/`: pure sync core. `planner.py` compares NAS, iCloud and the
  last common state (`state.py`, SQLite per account) using `rules.py` (decision
  table, delete brake); `executor.py` applies the plan; `trash.py`, `limits.py`
  (throttle). `core/photos/` + `photo_state.py`: Photos backup (plan, executor,
  integrity).
- `src/homeward/providers/`: `icloud_drive.py`/`drive_api.py`/`multipart.py`
  (streamed uploads), `icloud_photos.py`, `nas.py`, `fake*.py` for tests.
- `src/homeward/auth/`: pyicloud session and 2FA. `apple_errors.py`: Apple error mapping.
- `src/homeward/api/`: JSON API behind the DSM UI (`cgi.py`, `app.py`, `handlers/`).
- `src/homeward/daemon/scheduler.py`: interval scheduler, lanes per Apple ID.
- `src/homeward/cli.py`: `homeward` CLI (exit codes in docs/architecture.md).
- `package/`: DSM package (Ext.js UI in `package/ui`, scripts, conf);
  `tools/build_spk.py` builds the `.spk`; `spksrc/`: SynoCommunity recipe.
- Tests: `tests/unit/`, `tests/scenarios/` (decision table, bidirectional, photos).

## graphify (code knowledge graph)
The SessionStart hook (`.claude/hooks/session-start.sh`) rebuilds `graphify-out/`
from the checked-out code (AST only, no LLM, not committed). In cloud sessions it
also installs graphify and the dev dependencies.

- For codebase questions run `graphify query "<question>"` before grepping or
  reading source files; `graphify explain "<symbol>"` and
  `graphify path "<A>" "<B>"` for focused lookups. Read raw files only for lines
  the graph located. Put this rule in every subagent prompt that explores code.
- `graphify-out/GRAPH_REPORT.md` gives hubs and communities for broad reviews.
- After changing code run `graphify update .` to keep the graph current.
- If `graphify` is missing: `pip install graphifyy` (double y), then `graphify update .`.
- Skill `/graphify` lives in `.claude/skills/graphify/`.
