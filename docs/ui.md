# DSM UI and API

The DSM desktop app (`package/ui/desktop.js`) opens a window that hosts a plain
HTML page (`package/ui/index.html`, `app.js`, `homeward.css`; never name a file
`style.css` there, DSM loads that into the whole desktop). The page talks to one
JSON endpoint, `api.cgi`, which runs `python -m homeward.api.cgi` as the package user.
Strings live in `package/ui/texts/{enu,ger}/strings`; the page loads them itself.

## Security

Every API request must carry a DSM session: the page sends the DSM `SynoToken` as
`X-SYNO-TOKEN`, and `api/auth.py` validates cookie and token with DSM's
`authenticate.cgi`, then requires membership in `administrators`. Methods that
change anything only accept POST. The Apple ID password is passed to the sign-in
backend once and never written anywhere.

## Methods

| Method | POST | Parameters |
| --- | --- | --- |
| `status.get` | | |
| `pairs.list` | | |
| `pairs.create`, `pairs.update` | yes | `pair` (JSON object) |
| `pairs.delete` | yes | `name` |
| `pairs.run` | yes | `name`, `dry_run`, `allow_deletes` |
| `photos.defaults`, `photos.list` | | |
| `photos.create`, `photos.update` | yes | `photos` (JSON object with every `[[photos]]` option) |
| `photos.delete` | yes | `name` |
| `accounts.list` | | |
| `accounts.add` | yes | `name`, `apple_id`, `password` |
| `accounts.login` | yes | `name`, `password` |
| `accounts.verify` | yes | `name`, `code` |
| `accounts.remove` | yes | `name` |
| `log.runs` | | `limit` |
| `log.tail` | | `lines` |
| `log.events` | | `limit`, `level` (`info`, `warning`, `error`) |

Answers: `{"success": true, "data": ...}` or
`{"success": false, "error": {"code": "...", "message": "..."}}`; each code has a
text in the `[error]` section of the strings.

## Safety rules

- Config changes are checked by the core parser before they are written, the old
  file is copied to `var/config-backups/` (last 20 kept) and replaced atomically.
- Removing a pair, a photos backup or an account never touches files.
- Changing a pair's folders or account resets its sync state, so the next run is a
  first run (no deletes, no overwrites).
- The delete brake is approved per run from the status page ("Approve deletions").

## Files shared with the daemon (`homeward/runtime.py`)

Below `/var/packages/homeward/var` (`$HOMEWARD_VAR`):

- `config.toml`: accounts (`[[account]]` with `name`, `apple_id`) and pairs.
- `requests/<pair>.json`: "sync now" requests; the daemon calls `take_requests()`.
- `status.json`: written by the daemon, e.g. `{"running": "<pair>"}` or `{}` when idle.
- `homeward.log`: the daemon's log, shown on the log page.
- `events.jsonl`: the event log. Anything that runs in the background calls
  `runtime.record_event(level, source, code, message, until=...)` for waits, retries
  and errors (source `photos:<name>`, `pair:<name>`, `account:<name>` or `service`;
  codes with UI texts in `runtime.EVENT_CODES`). The status page shows running waits
  and the warnings and errors of the last 24 hours; the log page shows all events.

## DSM notifications

`homeward.dsm.notify.notify(Event.SYNC_FAILED, pair, count)` calls `synodsmnotify`
for all administrators with string keys from the `[notify]` section.

## Trying the page without a NAS

`python tools/ui_dev_server.py` serves the page with the real API, a demo sign-in
(two-factor code `123456`) and temporary data on http://localhost:8765/.
