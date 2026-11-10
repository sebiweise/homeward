# Running Homeward in Docker

Besides the DSM package, Homeward runs as a container on any Linux host (or on a
Synology through Container Manager). The image runs the same daemon and, if you set a
password, the same web page as the DSM app.

> Docker support is new. Keep an independent backup of anything you point it at.

## Quick start

```bash
git clone https://github.com/sebiweise/homeward.git
cd homeward
cp docker-compose.example.yml docker-compose.yml   # adjust the paths
mkdir -p config && sudo chown 1000:1000 config      # the container writes here
# Stops at the first failing step, so the password never lands in a readable file.
( set -e
  touch .env
  chmod 600 .env                                     # readable only by you
  # Asks for the password without showing it or letting the shell change it.
  IFS= read -rsp "Web password: " pw; echo
  if [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi  # end the last line first
  printf "HOMEWARD_WEB_PASSWORD='%s'\n" "$pw" >> .env )
docker compose up -d --build
```

`.env` gets mode 0600 before the password goes in (if that fails, nothing is
written), and the line is added, so other entries of an existing `.env` stay (remove
an older `HOMEWARD_WEB_PASSWORD` line first). The single quotes keep characters such
as `$` as they are, so the password must not contain a `'` itself.

Then open `http://127.0.0.1:8080/` on the host, sign in with `admin` and the password
from `HOMEWARD_WEB_PASSWORD`, add an Apple ID under **Apple IDs** and create photos
backups and sync pairs as in the [user guide](user-guide.md). The example compose file
publishes the page only on the host itself. From another device, use an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 <host>`, then `http://127.0.0.1:8080/`) or a reverse
proxy with HTTPS on the host (see below).

## Volumes

| Path in the container | Content |
| --- | --- |
| `/config` | `config.toml`, sync state, iCloud sessions, status, event log (keep it) |
| `/data/<name>` | the folders to back up into or sync; the folder picker lists them |

`/config` holds the trusted iCloud sessions: anyone who can read it can reach the
Apple ID, so do not share it.

The container runs as user `1000:1000`. Set `user:` in the compose file to the owner
of the folders you mount (on a Synology for example `1026:100`, see `id` over SSH), and
make sure that user owns `/config`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOMEWARD_WEB_PASSWORD` | (empty) | turns the web page on; empty runs headless |
| `HOMEWARD_WEB_USER` | `admin` | user name for the web page |
| `HOMEWARD_WEB_PORT` | `8080` | port of the web page inside the container |
| `HOMEWARD_VOLUMES` | `/data` | folders whose subfolders the folder picker offers (`:`-separated) |
| `TZ` | UTC | time zone for schedules, time windows and the Year/Month folders |

## Headless, with the config file only

Leave `HOMEWARD_WEB_PASSWORD` unset (remove its line from `docker-compose.yml`) and
write `config/config.toml` yourself (all options are in
[architecture.md](architecture.md)); paths are the ones inside the container. If the
container already ran with a password, `docker restart` keeps it: run
`docker compose up -d` so the container is created again without it.

```toml
state_dir = "state"

[daemon]
interval_minutes = 60

[[photos]]
name = "alice"
account = "alice"
apple_id = "alice@example.com"
target = "/data/photos/iCloud Alice"

[[pair]]
name = "docs"
account = "alice"
apple_id = "alice@example.com"
local = "/data/documents"
remote = "Documents"
mode = "download"
```

Sign in once (password and 2FA code are asked for interactively), then restart so the
daemon picks up the session right away:

```sh
docker exec -it homeward homeward login --account alice
docker restart homeward
```

The daemon logs to `docker logs homeward`; the event log is `config/events.jsonl`. The
command-line interface works the same way, for example
`docker exec homeward homeward photos --job alice --dry-run`.

## Differences from the DSM package

- **Sign-in**: HTTP Basic auth with one user and password instead of the DSM session.
  The page has no TLS of its own, so over plain HTTP anyone on the network can read
  the web password and the Apple ID password you type in. Reach it through a reverse
  proxy with HTTPS or an SSH tunnel, also in your LAN, let only that proxy (or
  `127.0.0.1`) reach port 8080, and never expose it to the internet as is. After 10
  wrong sign-ins with less than 5 minutes between them an address is locked out for 5
  minutes (shown in the event log). Behind a proxy or an SSH tunnel all clients share
  one address, so such a lock also keeps you out until it ends.
- **Notifications**: there are no DSM notifications; failures show up on the web page,
  in the event log and in the container log.
- **Trash**: deleted files go to `.homeward-papierkorb/` in the job's folder, that is
  on the host folder you mounted; it is emptied only after the days set as `trash_days`
  (0 = never). A
  Synology `#recycle` folder is used only if the mounted folder has one (a whole
  Synology share mounted at `/data/<share>`).
