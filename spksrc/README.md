# SynoCommunity package

This folder holds Homeward's package for [SynoCommunity](https://synocommunity.com/),
laid out like the [spksrc](https://github.com/SynoCommunity/spksrc) repository so it can
be copied into a fork of it unchanged:

| Path | Content |
| --- | --- |
| `spk/homeward/Makefile` | package definition: version, description, Python 3.12, service user, DSM app |
| `spk/homeward/src/service-setup.sh` | install, upgrade, uninstall and service hooks |
| `spk/homeward/src/requirements-*.txt` | Python dependencies: compiled ones are cross-compiled by spksrc, pure ones come from PyPI at installation |
| `spk/homeward/src/wizard/` | installation wizard (shared folder), English and German |
| `cross/homeward/` | downloads the release tarball and installs the DSM app (`package/ui`) |

It produces the same layout on the NAS as the package from `tools/build_spk.py`
(`/var/packages/homeward/{target/env,target/ui,var}`, user `sc-homeward`), so a user can
switch between the two packages and keep configuration and state.

The workflow `.github/workflows/spksrc.yml` builds this package for x64 and aarch64
with spksrc's own build container on every pull request that touches it.

## Submitting a release to SynoCommunity

Prerequisites: the release `vX.Y.Z` exists on GitHub and Homeward `X.Y.Z` is on PyPI
(both done by `.github/workflows/release.yml` when the tag is pushed).

1. Update this folder for the new version:
   - `SPK_VERS` and `CHANGELOG` in `spk/homeward/Makefile`; reset `SPK_REV` to 1 for a
     new version, raise it for a package-only change.
   - `PKG_VERS` in `cross/homeward/Makefile`.
   - `homeward==X.Y.Z` and the other pins in `requirements-pure.txt` and
     `requirements-crossenv.txt` (instructions at the top of `requirements-pure.txt`).
2. Fork [SynoCommunity/spksrc](https://github.com/SynoCommunity/spksrc), create a branch
   and copy the package into it:

   ```sh
   cp -R spksrc/spk/homeward  <spksrc-fork>/spk/
   cp -R spksrc/cross/homeward <spksrc-fork>/cross/
   cd <spksrc-fork>
   make -C cross/homeward digests      # downloads the release tarball, writes digests
   ```

3. Build and test locally with spksrc's container (see the
   [developer guide](https://docs.synocommunity.com/developer-guide/)), e.g.
   `make -C spk/homeward arch-x64-7.2`, and install the result on a real NAS: a fresh
   install and an upgrade must finish without errors in
   `/var/log/packages/homeward.log`.
4. Open the pull request as a draft titled `Homeward: initial package` (later
   `Homeward: update to vX.Y.Z`), fill in spksrc's template including the tested NAS
   model, architecture and DSM version, and mark it ready once its CI is green.
5. After a maintainer merges it, SynoCommunity's CI publishes the package and an admin
   activates it; it then appears in Package Center for everyone who added the
   SynoCommunity package source.
