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
