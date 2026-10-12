# ADR 0002: iCloud Drive provider (M3)

Date: 2026-10-12 · Status: accepted, two points pending the NAS probe

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

## Pending: `spikes/m3_etag_probe.py` on the NAS

1. Does a change four levels deep change every ancestor folder's etag? If yes,
   turn `trust_folder_etags` on.
2. Is a file's etag stable after upload (else every uploaded file would look changed
   on the next run)? Also checks rename with a new extension, moves, and a streamed
   big-file upload's peak RAM on the NAS.
