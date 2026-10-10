# Homeward – iCloud Sync for DSM

Homeward is a native Synology DSM 7.2 package that brings your iCloud data home:
iCloud Photos are backed up to the NAS, and iCloud Drive folders sync in either
direction (NAS → iCloud, iCloud → NAS, or bidirectional per folder pair).

> **Status:** early development (milestone M0/M1). Not usable yet.

## Goals

- Incremental iCloud Photos backup for multiple Apple IDs, including the shared library
- iCloud Drive sync per folder pair: `upload`, `download` or `bidirectional` (beta)
- Feels native in DSM: own app tile, settings, schedule, DSM notifications; no Docker, no root
- A tested sync core that also runs as a plain CLI

## Limitations

- Accounts with Advanced Data Protection are not supported.
- Interval-based sync, no real-time file system events.
- Homeward uses an unofficial Apple API via [pyicloud](https://github.com/timlaing/pyicloud);
  Apple changes can break it at any time.

## Development

```sh
python3.12 -m venv .venv && . .venv/bin/activate
make install
make lint
make test
```

See [`CLAUDE.md`](CLAUDE.md) for principles and conventions and
[`docs/architecture.md`](docs/architecture.md) for the design.

## License

MIT. Homeward is not affiliated with Apple or Synology.
