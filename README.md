# Homeward – iCloud Sync for Synology DSM

[![CI](https://github.com/sebiweise/homeward/actions/workflows/ci.yml/badge.svg)](https://github.com/sebiweise/homeward/actions/workflows/ci.yml)
[![Security](https://github.com/sebiweise/homeward/actions/workflows/security.yml/badge.svg)](https://github.com/sebiweise/homeward/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![DSM 7.2+](https://img.shields.io/badge/DSM-7.2%2B-0a5eb5)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)
[![Release](https://img.shields.io/github/v/release/sebiweise/homeward)](https://github.com/sebiweise/homeward/releases)

Homeward is a native Synology DSM 7.2+ package that brings your iCloud data home:

- **iCloud Photos backup**: originals, Live Photo videos, edits, albums and the Shared
  Library are copied to a NAS folder, incrementally, sorted by year and month.
- **iCloud Drive sync** per folder pair: `upload` (NAS → iCloud), `download`
  (iCloud → NAS) or `bidirectional` (beta).

It runs as an ordinary DSM package under its own unprivileged user: no Docker, no root,
no extra server.

> [!WARNING]
> Homeward is beta software (0.x). Keep an independent backup of anything you point
> it at.

## Features

- Incremental Photos backup for several Apple IDs, including the iCloud Shared Library
  and shared albums; albums and favorites as hard links that take no extra space;
  optional XMP sidecars with captions, keywords and location.
- Drive sync with a three-way comparison (NAS, iCloud, last common state), conflict
  copies instead of overwrites, and streamed uploads that keep memory use low even for
  multi-gigabyte files.
- **Never loses data**: nothing is overwritten or deleted without a recoverable copy.
  NAS deletions go to the Synology recycle bin (or Homeward's own trash), iCloud
  deletions to "Recently Deleted". The first run of a pair never deletes, and a
  *delete brake* stops any run that would delete more than 50 files, or more than 10 %
  of them once it deletes more than 5.
- Dry runs that show the plan without changing anything.
- Interval scheduler as a DSM service; every wait, retry and error shows up in the log.
- A DSM app (German and English) for accounts with 2FA sign-in, photos backups, sync
  pairs, status and log, plus DSM notifications when a sync fails.
- A plain command-line interface for everything.

## Requirements

- Synology NAS with DSM 7.2 or later on x86_64 or aarch64 (any model for which
  SynoCommunity offers Python 3.12).
- The [SynoCommunity](https://synocommunity.com/) package **Python 3.12** (`python312`).
- An Apple ID with two-factor authentication. Accounts with **Advanced Data Protection**
  are not supported, because Apple does not offer web access to their data.

## Installation

1. Add the SynoCommunity package source and install **Python 3.12** from it.
2. Download `homeward-<version>-x64.spk` or `homeward-<version>-aarch64.spk` from the
   [releases](https://github.com/sebiweise/homeward/releases).
3. In DSM open **Package Center › Manual Install**, choose the file and confirm the
   warning about the unknown publisher.

The package creates the shared folder `homeward`, which the package user `sc-homeward` may
read and write. To sync another shared folder, grant the user `sc-homeward` read/write
access to it in **Control Panel › Shared Folder**.

Homeward is on its way into the SynoCommunity package source; once it is there, it
installs and updates directly from Package Center (see [spksrc/](spksrc/)).

See the [user guide](docs/user-guide.md) for setup, sign-in and configuration.

## Quick start

Open **Homeward** from the DSM main menu (administrators only):

1. **Accounts**: add your Apple ID, enter the password and the 2FA code from one of
   your Apple devices. Homeward keeps only the trusted session, never the password.
2. **Photos**: add a backup with a target folder, e.g. `/volume1/homeward/Photos`.
3. **Sync pairs**: connect a NAS folder with an iCloud Drive folder and pick
   `upload`, `download` or `bidirectional`.
4. **Status**: start a test run to see what would happen, then *Sync now* or let the
   schedule take over (every 60 minutes by default). **Log** shows every run, wait and
   error.

Everything can also be done from the command line over SSH; see the
[user guide](docs/user-guide.md#command-line).

## Documentation

- [User guide](docs/user-guide.md): installation, sign-in, configuration, CLI, troubleshooting, known issues
- [DSM UI and API](docs/ui.md): the DSM app, its JSON API and security model
- [Architecture](docs/architecture.md): sync core, decision tables, safety nets, providers
- [Architecture decision records](docs/adr/)
- [Changelog](CHANGELOG.md)

## Limitations

- Homeward uses Apple's unofficial web API through
  [pyicloud](https://github.com/timlaing/pyicloud). Apple can change it at any time,
  which can break sync until Homeward is updated.
- Sync is interval based; there are no real-time file system events.
- iCloud Drive does not distinguish upper and lower case, so `a.txt` and `A.txt` in
  the same NAS folder cannot both be uploaded.
- Photos: people, faces and memories are not available through the API.

## Contributing

Bug reports, ideas and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first; security issues go through
[SECURITY.md](SECURITY.md).

## Acknowledgements

- [pyicloud](https://github.com/timlaing/pyicloud) for access to iCloud.
- [SynoCommunity](https://synocommunity.com/) for Python on DSM.
- [iCloudPhotoSync](https://github.com/Euphonique/iCloudPhotoSync) for inspiration.

## License

[MIT](LICENSE). Homeward is not affiliated with, endorsed by or sponsored by Apple Inc.
or Synology Inc. iCloud is a trademark of Apple Inc.; Synology and DSM are trademarks
of Synology Inc.
