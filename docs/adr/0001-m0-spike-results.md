# ADR 0001: M0 spike results on the NAS

Date: 2026-10-11 · Status: accepted

Measured with `spikes/m0_spike.py` on the maintainer's NAS (x86_64, DSM 7.4.1, 11.8 GB RAM),
Python 3.12.13 from SynoCommunity, pyicloud 2.7.0.

## Findings

| Question | Result |
| --- | --- |
| Install on DSM | All native deps (`cryptography`, `pydantic-core`, `protobuf`, `cffi`) install as prebuilt x86_64 wheels. pyicloud 2.7.0 imports `rich` without declaring it. |
| Login | Password + 2FA works; trusted session persists in the cookie directory. |
| Small roundtrip | Upload, list, streamed download, SHA-256 match, move to trash: OK. |
| 1 GB upload | 198 s (~5.2 MB/s, likely the uplink). Peak RSS 2.1 GB: pyicloud builds the whole multipart body in memory. |
| 1 GB download | 69 s (~14.8 MB/s), streamed in chunks, SHA-256 match. |
| 5 GB file | Works: upload 869 s (~5.9 MB/s), download 205 s (~25 MB/s), SHA-256 match. Peak RSS 9.97 GB (~2x file size again), close to the NAS's 11.8 GB total. No server-side size limit hit at 5 GB. |
| Delta API | pyicloud exposes no change feed for Drive. |
| Folder etags | Stable across runs with no changes; a new file changes only its folder's etag. |
| Drive root | Contains `app_library` nodes (e.g. Scriptable, Shortcuts) besides folders. |

## Decisions

- Drive provider streams uploads itself instead of calling `DriveNode.upload` (M3).
- Remote listing walks the tree but skips folders whose etag is unchanged since the last sync.
- `rich` is pinned as an explicit dependency alongside pyicloud until upstream declares it.
- `app_library` nodes are excluded by default; whether to offer them is decided in M3.

## Open

- Whether a change deep in the tree also changes the etags of its ancestor folders
  (only top-level folders were tested). Verify in M3 before relying on skipping parents;
  see ADR 0002.
- aarch64 install not yet tested.
