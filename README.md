# Homeward – iCloud Sync for Synology DSM

[![CI](https://github.com/sebiweise/homeward/actions/workflows/ci.yml/badge.svg)](https://github.com/sebiweise/homeward/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![DSM 7.2+](https://img.shields.io/badge/DSM-7.2%2B-0a5eb5)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)

Homeward is a native Synology DSM 7.2+ package that brings your iCloud data home:

- **iCloud Photos backup**: originals, Live Photo videos, edits, albums and the Shared
  Library are copied to a NAS folder, incrementally, sorted by year and month.
- **iCloud Drive sync** per folder pair: `upload` (NAS → iCloud), `download`
  (iCloud → NAS) or `bidirectional` (beta).

It runs as an ordinary DSM package under its own unprivileged user: no Docker, no root,
no extra server.

> [!WARNING]
> Homeward is in early development and has not had a stable release yet. Keep an
> independent backup of anything you point it at.

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
  *delete brake* stops any run that would delete more than 10 % or more than 50 files.
- Dry runs that show the plan without changing anything.
- Interval scheduler as a DSM service, status file and log for every run.
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

The package creates the shared folder `homeward`, which the package user `homeward` may
read and write. To sync another shared folder, grant the user `homeward` read/write
access to it in **Control Panel › Shared Folder**.

See the [user guide](docs/user-guide.md) for setup, sign-in and configuration.

## Quick start

Until the DSM app is finished, Homeward is set up over SSH:

```sh
# 1. Add a photos job and/or a Drive pair
sudo vi /var/packages/homeward/var/config.toml

# 2. Sign in once with password and 2FA code
sudo -u homeward env HOME=/var/packages/homeward/var homeward login --account alice

# 3. Check the plan, then let the daemon take over (it runs every 60 minutes)
sudo -u homeward env HOME=/var/packages/homeward/var homeward sync --dry-run
```

```toml
[[photos]]
name = "alice"
account = "alice"
apple_id = "alice@example.com"
target = "/volume1/homeward/Photos"

[[pair]]
name = "documents"
account = "alice"
apple_id = "alice@example.com"
local = "/volume1/homeward/Documents"
remote = "Documents"
mode = "download"
```

## Documentation

- [User guide](docs/user-guide.md): installation, sign-in, configuration, CLI, troubleshooting
- [Architecture](docs/architecture.md): sync core, decision tables, safety nets, providers
- [Architecture decision records](docs/adr/)

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
