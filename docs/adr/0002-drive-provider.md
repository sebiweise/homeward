# ADR 0002: iCloud Drive provider (M3)

Date: 2026-10-12 · Status: accepted

## Decisions

- The provider talks to Drive through `DriveApi` (`providers/drive_api.py`). The real
  implementation reuses pyicloud's session, URLs and parameters (private API, hence
  the `<2.8` pin) and sends uploads itself as a streamed multipart body. Locally a
  1 GB upload through `requests` peaked at 35 MB RSS, with `Content-Length` set (no
  chunked encoding) and a body byte-identical to what `requests` builds for `files=`.
- Listing fetches folders in batches of 50 per `retrieveItemDetailsInFolders` call.
- Skipping unchanged subtrees by folder etag is implemented but off by default
  (`trust_folder_etags`). With etags that only change on the direct parent (all M0
  showed), skipping would silently miss deep changes.
- File version = `docwsid:etag`, recorded from a fresh listing after each upload.
- `app_library` nodes and any other non-file, non-folder node are skipped (logged).
- NAS deletions use the share's `#recycle` when enabled, else the package trash with
  a 30-day purge (`trash_days` per pair).
- iCloud sign-in is not part of M3; the CLI gets a signed-in `DriveApi` through
  `cli.drive_api_factory`, which the auth work (M2) fills in.

## Probe results (NAS, 2026-10-12)

| Check | Result |
| --- | --- |
| New, replaced or trashed file four levels deep | etag changes on every ancestor up to the Drive root |
| File renamed in place | **no** folder etag changes, not even its own folder's |
| File etag after upload, 30 s later | stable (`docwsid:etag` recorded = listed) |
| File moved / renamed | same `docwsid`, new etag |
| 1 GB streamed upload | 205 s, peak RSS 80 MB (pyicloud: 2.1 GB), SHA-256 match |

Consequences:

- `trust_folder_etags` stays off: a folder's etag misses renames inside it, so a
  cached listing would keep the old name. Every run walks the whole tree (batched).
- The version token `docwsid:etag` is stable after upload, so uploaded files are
  not re-uploaded. `docwsid` survives moves and renames, which lets bidirectional
  sync (M4) recognise renames in iCloud.
- Streaming the upload body fixes the memory problem from ADR 0001.
- Open: whether an in-place edit (iPhone/Mac) changes the file's etag; the probe's
  `snapshot`/`compare` now records file versions to check this.
