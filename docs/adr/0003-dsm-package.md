# ADR 0003: DSM package build and runtime

Date: 2026-10-12 · Status: accepted

## Context

Homeward ships as a native DSM 7.2+ package for x64 and aarch64 without root. The plan
assumed spksrc for native wheels (`cryptography`, `pydantic-core`, `protobuf`, `cffi`,
`charset-normalizer`). M0 showed these install as prebuilt wheels on the NAS.

## Decisions

- **No spksrc.** Every dependency has a `manylinux2014` wheel for CPython 3.12 on both
  x86_64 and aarch64; DSM 7.2 ships glibc 2.36, well above manylinux2014's 2.17.
  `tools/build_spk.py` (stdlib only, `make spk ARCH=x64|aarch64`) builds the Homeward
  wheel, downloads all dependency wheels for the target platform, fails on any sdist or
  wrong-platform wheel, and assembles the `.spk`. If a future dependency lacks a wheel,
  spksrc's wheel cross-build is the fallback.
- **Runtime:** depends on SynoCommunity `python312` (`install_dep_packages`). postinst
  builds `/var/packages/homeward/target/env` offline from the bundled wheelhouse; the
  venv is rebuilt on every install and upgrade.
- **Version:** from the git tag only. `vX.Y.Z` with N commits on top gives the DSM version
  `X.Y.Z-N` (`0.0.0-N` before the first tag).
- **Architectures:** INFO `arch` lists the platform names of SynoCommunity's DSM 7.2
  toolchains, i.e. exactly where `python312` is available.
- **User and data:** everything runs as the package user `homeward` (`run-as: package`).
  The `data-share` resource creates the share `homeward` with read/write for that user;
  other shares need the user granted in Control Panel. `usr-local-linker` puts the
  `homeward` CLI into `/usr/local/bin`.
- **Persistent files** live in `/var/packages/homeward/var` (kept on upgrade): `config.toml`,
  `state/`, `status.json`, `homeward.log`. A real uninstall first copies config and state
  to `homeward/.homeward-backup/<timestamp>/` in the share; synced files are never touched.
- **Daemon:** `homeward-daemon` runs each pair and `[[photos]]` job every `[daemon] interval_minutes` (default
  60, minimum 5) as its own `homeward sync --pair` process, so memory used by large
  transfers is freed after each run (`homeward photos --job` for photos jobs). It re-reads the config every cycle, keeps
  `status.json` for the UI (`running` plus last result and next run per pair), runs
  "sync now" requests from `var/requests/<pair>.json` (the M6 UI contract; dry runs do
  not move the schedule), runs everything at once on SIGUSR1, and on SIGTERM stops the
  running sync (finished steps are recorded, the rest is planned again next run).

## Open

- aarch64 install is built and checked for wheel platforms in CI but not installed on
  real hardware yet.
- The DSM app (`dsmuidir`, M6) is added to INFO as soon as `package/ui/config` exists.
