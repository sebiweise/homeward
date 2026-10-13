# Changelog

All notable changes to Homeward are listed here. Versions follow
[Semantic Versioning](https://semver.org/); the version comes from the git tag.

## [0.1.0] - 2026-10-13

First public release. Homeward is still beta: keep an independent backup of
anything you point it at.

### iCloud Photos backup

- Incremental backup of one or more Apple IDs into NAS folders sorted by year and
  month, including the iCloud Shared Library and shared albums.
- Live Photo videos, RAW+JPEG pairs, edited versions and XMP sidecars (caption,
  keywords, location, favorite), each optional.
- Album and favorites folders as hard links that take no extra space.
- Never deletes: photos removed in iCloud stay on the NAS.
- Expired download links of shared albums are renewed automatically; a run that ends
  because iCloud is still preparing the library shows as "Waiting for iCloud" and is
  retried on the next run, without a failure notification.

### iCloud Drive sync

- Folder pairs in `upload`, `download` or `bidirectional` mode (bidirectional needs
  `beta = true` on the pair).
- Three-way comparison with conflict copies instead of overwrites; renames and moves
  are recognised.
- Streamed uploads with low memory use, also for multi-gigabyte files.
- Safety nets: every overwrite or delete keeps a recoverable copy (Synology recycle bin,
  Homeward's own trash or iCloud's "Recently Deleted"), the first run of a pair never
  deletes, a delete brake stops runs that would delete too much, and dry runs show the
  plan first.

### DSM package and app

- Native DSM 7.2 package for x64 and aarch64, running as the unprivileged user
  `sc-homeward`; depends on SynoCommunity's Python 3.12.
- DSM app in English and German: accounts with two-factor sign-in, photos backups,
  sync pairs, status with "sync now", test run and delete approval, and a log of every
  run, wait and error. DSM notifications when a sync fails.
- Command-line interface (`homeward sync`, `homeward photos`, `homeward login`) and an
  interval scheduler service.

[0.1.0]: https://github.com/sebiweise/homeward/releases/tag/v0.1.0
