# Changelog

All notable changes to Homeward are listed here. Versions follow
[Semantic Versioning](https://semver.org/); the version comes from the git tag.

## [Unreleased]

### Added

- Photos backups have an option *Also delete photos on the NAS that were deleted in
  iCloud (1:1)* (`mirror_deletes`, off by default). Deleted photos and their album links
  move to the share's Synology recycle bin when it is enabled (DSM's emptying schedule
  applies), otherwise to the backup folder's trash, which is emptied after `trash_days`
  (30). The delete
  brake holds back more than 50 or more than 10 % of the photos until approved; photos
  that only left the selection (libraries, file types) are never deleted.
- *Clean up deleted photos (N)* on a photos backup's overview removes the photos deleted
  in iCloud that are still on the NAS, into the same trash (`photos --remove-gone`).
- While a photos backup or sync runs, its overview shows a progress card: the current
  step (signing in, reading the library, reading albums, downloading, album links), how
  many of how many are done, data, speed, time left, the last file and the latest log
  message, including waits for Apple. The page refreshes every 3 s while a job runs.

### Changed

- "Waiting for iCloud": a library zone Apple is still indexing only holds up a photos
  backup when the backup includes it (pyicloud gave up on any zone, even ones no backup
  reads). The log names the library and Apple's progress, e.g. "the Shared Library
  (73 %)". A run waits at most 3 minutes for it instead of 8, so other jobs are not held
  up, and the daemon tries a waiting job again after 15 minutes instead of a whole
  interval.
- A photos backup reads only what changed in iCloud when it safely can (from iCloud's
  change feed) instead of the whole library, which took about 20 minutes for 28,000
  photos. It still reads the whole library on the first run, after a run with errors,
  when a file is missing on the NAS, when albums, settings or shared albums changed,
  whenever iCloud's answer is incomplete, and at least once every 24 hours; that run
  also marks photos removed in iCloud and updates album folders.
- Photo downloads no longer pause after every 100 files: the next batch's download
  links are renewed and queued while the current batch still downloads.

### Fixed

- Photos (and files from iCloud Drive) now show up in Synology Photos. Homeward used
  to hard-link a finished download into place, which DSM's indexer does not notice;
  it now renames it into place, still never replacing an existing file. Photos backed
  up before this fix need a one-time re-index in Synology Photos.

## [0.2.0] - 2026-10-15

### Added

- Photos backups have a switch *Own folders for the Shared Library and shared albums*
  (`source_folders`, on by default). Off, a backup of just the Shared Library or just
  the shared albums puts its photos straight into the NAS folder. Photos already backed
  up keep their place.

### Changed

- The DSM app is laid out like Cloud Sync: a sidebar with every sync pair and photos
  backup, and per task the tabs Overview, Runs, Settings and Log. *Runs* lists each run
  with its result, what it did and the waits, retries and errors during it.
- Each run records a summary (files uploaded, downloaded, up to date, failed) in the
  event log.
- The notices on the status page leave out pairs and photo backups that were
  deleted, and anything a later successful run of the same job has settled.
- *Clear notices* hides the current notices on the status page. The event log on
  the Log tab keeps every event.
- *Sign in again* is offered for every Apple ID, also while it shows as signed in. It
  signs in with password and code from scratch, without the stored cookies, and
  replaces the stored session only once that worked. Sync pairs, photo backups and
  their state stay; nothing has to be deleted.
- When Apple rejects a stored session during a run (for example 421 "Invalid global
  session"), the Apple ID now shows *Sign-in required* instead of a generic error.
  If it comes back right after signing in again, the message and the new *Known
  issues* section of the user guide explain the Apple account settings to check.

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
- Folder picker for NAS folders: browse the shared folders Homeward can open and
  create a new folder in place.
- Cancel a queued or running sync from the status page; the run stops cleanly and
  keeps every finished step.
- Configurable schedule: a global sync interval on the status page, and an optional
  own interval per sync pair and photos backup.
- Command-line interface (`homeward sync`, `homeward photos`, `homeward login`) and an
  interval scheduler service.

[0.2.0]: https://github.com/sebiweise/homeward/releases/tag/v0.2.0
[0.1.0]: https://github.com/sebiweise/homeward/releases/tag/v0.1.0
