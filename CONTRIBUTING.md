# Contributing to Homeward

Thanks for helping! Homeward handles people's photos and files, so the bar for
correctness is high. Please read this before opening a pull request.

## Reporting bugs and asking for features

- Use the [issue templates](https://github.com/sebiweise/homeward/issues/new/choose).
- Search existing issues first.
- Attach the relevant part of `/var/packages/homeward/var/homeward.log` and remove
  Apple IDs, names and paths you do not want to share. Never post passwords, 2FA codes
  or the contents of the `state/sessions/` folder.
- Security problems: see [SECURITY.md](SECURITY.md), not a public issue.

## Development setup

You need Python 3.12. No NAS and no Apple account are needed for the tests: they run
against fake providers.

```sh
git clone https://github.com/sebiweise/homeward.git
cd homeward
python3.12 -m venv .venv && . .venv/bin/activate
make install      # pip install -e ".[dev]"
make lint         # ruff check, ruff format --check, mypy --strict
make test         # pytest
make spk ARCH=x64 # optional: build dist/homeward-<version>-x64.spk
```

To talk to a real iCloud account, install the extra: `pip install -e ".[dev,icloud]"`.

## Principles

These are not negotiable; reviews check them:

- **Never lose data.** No overwrite or delete without a recoverable copy.
- `src/homeward/core` must not import DSM or pyicloud code
  (enforced by `tests/unit/test_core_isolation.py`).
- All iCloud access goes through adapters in `src/homeward/providers/`.
- Every rule in the sync decision tables has a scenario test in
  `tests/scenarios/test_decision_table.py`.

## Conventions

- Python 3.12 with type hints; `mypy --strict` and `ruff` must pass.
- Code, comments and docs in English. UI strings live in
  `package/ui/texts/{enu,ger}/strings`; add both languages.
- The version comes from the git tag only (hatch-vcs); do not hard-code it.
- Keep pull requests focused. Explain *what* changes for users and *why*.
- Add or update tests with every behaviour change. Changes to how files are planned,
  transferred or deleted need a scenario test.
- Larger design decisions get an ADR in `docs/adr/`.

[`CLAUDE.md`](CLAUDE.md) holds the same rules in short form for AI coding assistants.

## Pull requests

1. Fork and create a branch from `master`.
2. Make your change with tests and docs.
3. Run `make lint` and `make test`.
4. Open a pull request and fill in the template. CI must be green.

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE) and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Releases

1. Add the version's section to [CHANGELOG.md](CHANGELOG.md) and merge it.
2. Push a tag `vX.Y.Z` on `master`. The release workflow builds the x64 and aarch64
   packages, publishes the Python package to PyPI and creates the GitHub release with
   the changelog section as notes.
3. Update the SynoCommunity package and submit it as described in
   [spksrc/README.md](spksrc/README.md).
