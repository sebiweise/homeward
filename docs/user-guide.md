# User guide

This guide covers installing Homeward on a Synology NAS, signing in to iCloud,
configuring photos jobs and Drive pairs, and finding out what Homeward did. For how
the sync works internally, see [architecture.md](architecture.md).

> Homeward is in early development. Keep an independent backup of anything you point
> it at.

## Installation

1. In **Package Center › Settings › Package Sources**, add SynoCommunity
   (`https://packages.synocommunity.com/`) and install **Python 3.12**.
2. Download the package for your NAS from the
   [releases](https://github.com/sebiweise/homeward/releases):
   `homeward-<version>-x64.spk` for Intel/AMD models, `homeward-<version>-aarch64.spk`
   for ARM64 models. `uname -m` over SSH prints `x86_64` or `aarch64`.
3. **Package Center › Manual Install**, choose the `.spk` and confirm the warning about
   the unknown publisher.

After the installation:

| Path | Content |
| --- | --- |
| `/var/packages/homeward/target/env` | Python environment (rebuilt on every upgrade) |
| `/var/packages/homeward/var/config.toml` | configuration |
| `/var/packages/homeward/var/state/` | sync state and the iCloud sessions |
| `/var/packages/homeward/var/status.json` | last result and next run of every job |
| `/var/packages/homeward/var/homeward.log` | log |
| `/usr/local/bin/homeward` | command-line interface |

The package also creates the shared folder `homeward`. Everything runs as the package
user `sc-homeward`, which can only write where it has been granted access. To use another
shared folder, give the user `sc-homeward` read/write permission in **Control Panel ›
Shared Folder › Edit › Permissions** (switch the list to *System internal user*).

Updating works the same way as the installation; configuration and state are kept.
Uninstalling copies configuration and state to `homeward/.homeward-backup/<timestamp>/`
first. Synced files are never touched by an uninstall.

## Using the DSM app

Open **Homeward** from the DSM main menu. Only members of the `administrators` group
can use it.

The app is laid out like DSM's Cloud Sync. The sidebar lists every sync pair and photos
backup with a colored dot for its state; the switch at the bottom shows the Apple IDs
instead. *+ Add* creates a sync pair, a photos backup or (in the Apple ID list) an
Apple ID, and *Service* shows the background service.

| Tab | What you see there |
| --- | --- |
| Overview | State, last and next run, folders and schedule of the selected task; *Sync now* and *Manage* (*Test run*, which changes nothing, or *Cancel*), *Approve deletions* after the delete brake stopped a run. Waits and errors of the last 24 hours are listed as notices, without anything a later successful run settled; *Clear notices* hides them (the event log keeps them). |
| Runs | Every run of the task. Pick one to see when it ran, how it ended, what it did (files uploaded, downloaded, up to date, failed) and every wait, retry or error during it. |
| Settings | The task's settings, with *Edit* and *Delete*. |
| Log | The task's event log, filtered by level. Under *Service*, the log tab shows the events of all tasks and the raw service log. |

*Service* also answers "Is everything backed up?": every task with its last successful
run, its number of files and their size. A task without a successful run for more than
3 days (or, never successful, 3 days after its first run) is marked there, writes a
warning to the event log and sends one DSM notification until it succeeds again.

A run's details list the files it deleted, replaced, kept twice (conflict copies) or
moved to another library, up to 200 of them; the service log has the complete list.
Errors from Apple come with a line that explains them in words (wrong password, locked
Apple ID, web access to iCloud data turned off, Apple throttling or down, no network).

Jobs of different Apple IDs run side by side (two at a time); jobs of the same Apple ID
take turns, since they share one iCloud session.

Homeward keeps only the trusted session Apple hands out after the 2FA step (in
`state/sessions/<account>/`, readable only by the package user), never the password.
When Apple expires the session, the affected jobs fail and the Apple ID list asks you to
sign in again. DSM notifications tell you when a sync fails, the delete brake stops a
run, an Apple ID needs a new sign-in (once per Apple ID, not once per job), a
bidirectional sync created conflict copies, or a task has not been backed up for 3 days.
The sessions are left out of the backup the package keeps when it is uninstalled, so a
reinstall asks for a new sign-in.

NAS folders are picked with *Browse...*: it lists the shared folders the user
`sc-homeward` can open, and *New folder* creates one in place. A share missing there
needs read/write permission for `sc-homeward` (see above). Typing the full path still
works.

Every change in the app is checked before it is saved; the previous configuration is
kept in `var/config-backups/` (the last 20). Removing a pair, a photos backup or an
account never touches any files.

## Command line

Everything the app does also works over SSH. Enable SSH in **Control Panel ›
Terminal & SNMP**, log in as an administrator and run the CLI as the package user so
that state and sessions keep the right owner:

```sh
alias hw='sudo -u sc-homeward env HOME=/var/packages/homeward/var homeward'
hw --version
hw login --account alice --apple-id alice@example.com   # password, then 2FA code
```

The examples below use this `hw` alias.

## Configuration

The DSM app writes the configuration for you. It is a TOML file at
`/var/packages/homeward/var/config.toml`, which you can also edit by hand (check the
result with a dry run). The daemon reads it again before every cycle, so no restart is
needed. The options below are the same ones the app offers.

```toml
state_dir = "/var/packages/homeward/var/state"

[daemon]
interval_minutes = 60     # every job runs once per interval (5 to 10080 minutes)
```

*Service* sets this interval under "Change". A sync pair or
photos backup can also have its own `interval_minutes` (same range, field "Own
interval" in its dialog); without one it follows the global schedule. A new photos
backup in the app starts with 30 minutes.

Two more optional keys apply to both sync pairs and photos backups (fields *Time window*
and *Bandwidth* in their dialogs):

| Key | Default | Meaning |
| --- | --- | --- |
| `window` | none | daily hours in NAS time, e.g. `"22:00-06:00"`: scheduled runs start only then; when the window closes, a run pauses, keeps what it transferred and goes on when the window opens. *Sync now* always runs. |
| `max_rate_kb` | none | at most this many KB/s for the job's downloads and uploads (shared by its parallel downloads) |

### Photos backup

One `[[photos]]` job backs up one Apple ID into one NAS folder. Photos only flow from
iCloud to the NAS; Homeward never deletes anything in iCloud. A photo deleted in iCloud
stays on the NAS, unless `mirror_deletes` is on (see below).

```toml
[[photos]]
name = "alice"
account = "alice"
apple_id = "alice@example.com"
target = "/volume1/homeward/Photos"
```

Optional settings with their defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `libraries` | `["personal", "shared", "shared_albums"]` | which libraries to back up |
| `folder_scheme` | `"{year}/{month}"` | folders by capture date; `{year}`, `{month}`, `{day}` |
| `albums` | `true` | album folders with hard links to the originals |
| `album_names` | `[]` | only these albums become folders (one name per line in the dialog); empty: all |
| `favorites` | `true` | favorites folder with hard links |
| `videos` | `true` | videos |
| `live_videos` | `true` | the video part of Live Photos |
| `raw_alternative` | `true` | both files of RAW+JPEG shots |
| `edited` | `true` | edits as `Name (bearbeitet).ext` next to the original |
| `xmp` | `true` | XMP sidecars for photos with caption, keywords, location or favorite |
| `hidden` | `false` | hidden photos |
| `workers` | `4` | parallel downloads, 1 to 8 |
| `mirror_deletes` | `false` | 1:1 backup: photos deleted in iCloud are deleted on the NAS too (into the trash) |
| `max_delete_ratio`, `max_delete_count` | `0.1`, `50` | delete brake for `mirror_deletes` |
| `trash_days` | `30` | with `mirror_deletes`: days the trash keeps deleted photos, `0` forever |
| `shared_folder` | `"Geteilte Mediathek"` | folder for the Shared Library |
| `albums_folder` | `"Alben"` | folder for albums |
| `favorites_folder` | `"Favoriten"` | folder for favorites |
| `hidden_folder` | `"Ausgeblendet"` | folder for hidden photos |
| `shared_albums_folder` | `"Geteilte Alben"` | folder for shared albums |
| `source_folders` | `true` | `false` puts the Shared Library and shared albums straight into the target folder, for a backup of just one of them |

The dialog keeps `workers`, the delete brake and `trash_days` under *Advanced settings*.
Albums belong to the Apple ID, not to a library, and can hold photos of the Shared
Library, so a backup of only the Shared Library still reads all albums while `albums`
is on; switch it off, or pick albums, to skip that.

A photo moved between the personal and the Shared Library in iCloud is moved on the NAS
too (into the other library's folder) instead of being downloaded again.

Once a week a run checks that every backed-up photo still has the size it was
downloaded with. A damaged file (cut off, overwritten) goes to the trash with its album
links and is downloaded again; one iCloud no longer has stays as it is, with a warning.

Switching an option off later keeps the files it already produced. Folder changes
apply to newly downloaded photos; photos already on the NAS keep their place.

With `mirror_deletes`, a run that reads the whole library (at least once a day) moves
photos deleted in iCloud, and their album links, to the trash: the share's Synology
recycle bin (`#recycle`, same path below it as in the share) when it is enabled for the
share, so DSM's own emptying schedule applies; otherwise `<target>/.homeward-papierkorb/<date>/`,
whose day folders older than `trash_days` are emptied. Photos in iCloud's "Recently Deleted"
count as deleted. A run that would delete more than `max_delete_count` photos or more
than `max_delete_ratio` of them (the share only counts from more than 5 deletions on)
only holds those deletions back (the rest of the backup runs) until you approve them with *Approve deletions* or `--allow-deletes`. Photos that
only drop out because you changed the libraries or file types are not deleted, and
neither are photos that were already marked deleted before the option was switched on.

Photos deleted in iCloud that are still on the NAS (marked deleted by earlier runs, with
or without `mirror_deletes`) are counted on the backup's overview. *Clean up deleted
photos* (or `homeward photos --remove-gone`) backs up now and moves them and their album
links to the same trash. Photos that are back in iCloud by then are kept.

### iCloud Drive pairs

One `[[pair]]` connects one NAS folder with one iCloud Drive folder.

```toml
[[pair]]
name = "documents"
account = "alice"
apple_id = "alice@example.com"
local = "/volume1/homeward/Documents"
remote = "Documents"              # path below the iCloud Drive root
mode = "download"                 # upload | download | bidirectional
exclude = ["*.tmp", "Archive"]
```

| Mode | Behaviour |
| --- | --- |
| `upload` | NAS → iCloud. The NAS wins; files that only exist in iCloud are left alone. |
| `download` | iCloud → NAS. iCloud wins; files that only exist on the NAS are left alone. |
| `bidirectional` | Changes flow both ways; a file changed on both sides keeps both versions as a conflict copy. Beta: needs `beta = true` in the pair (the DSM app sets it when you pick this mode). |

Optional settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `exclude` | `[]` | glob patterns, matched against every file and folder name in a path |
| `max_delete_ratio` | `0.10` | delete brake: stop if a run would delete more than this share of files (only counts from more than 5 deletions) |
| `max_delete_count` | `50` | delete brake: stop if a run would delete more files than this |
| `trash_days` | `30` | days to keep files in Homeward's NAS trash, `0` keeps them forever |

Synology system folders (`@eaDir`, `#recycle`, …), `.DS_Store` and temporary files are
always excluded.

In `bidirectional` mode a rename or move on either side is carried out as "old name to
the trash, new name copied over" and does not count for the delete brake. iCloud Drive
does not keep names apart that differ only in upper/lower case (`notes.txt`,
`Notes.txt`); such a NAS file is not uploaded and shows up as skipped in every run.

## Safety nets

- Every overwrite or delete first moves the old version somewhere recoverable: the
  share's recycle bin `#recycle` if it is enabled, otherwise
  `.homeward-papierkorb/<date>/` in the pair's folder; in iCloud, "Recently Deleted".
- The first run of a pair never deletes or overwrites anything.
- The delete brake stops a run that would delete too much. The share
  (`max_delete_ratio`) only counts from more than 5 deletions, so deleting 2 of 10 files
  goes through. Check the plan with a dry run and approve it once with *Approve
  deletions* on the pair's overview or `hw sync --pair NAME --allow-deletes`.
- A run stops before changing anything when a folder it synced before is gone (a
  renamed or deleted folder, a shared folder that is not mounted, an empty iCloud Drive
  answer): an empty side is never read as "delete everything". It runs again as soon as
  the folder is back.
- File names iCloud allows but DSM treats specially (starting with `.`, `@` or `#`) are
  stored on the NAS with a leading `_`.

## Command reference

```sh
hw sync [--pair NAME] [--dry-run] [--allow-deletes]   # Drive pairs
hw photos [--job NAME] [--dry-run] [--allow-deletes] # Photos jobs
hw login --account NAME [--apple-id ID]               # sign in with 2FA
```

`--dry-run` prints what would happen and changes nothing. All commands accept
`--config PATH` (default: `$HOMEWARD_CONFIG` or the package's `config.toml`).

The daemon runs every job once per interval (its own, or the global one). To run all jobs now:

```sh
sudo kill -USR1 "$(sudo cat /var/packages/homeward/var/homeward.pid)"
```

## Status and troubleshooting

The app's overview, runs and log tabs show the same information as these files:

```sh
sudo cat /var/packages/homeward/var/status.json      # last result and next run per job
sudo tail -n 50 /var/packages/homeward/var/homeward.log
```

The service log rotates at 5 MB and keeps three older files (`homeward.log.1` to `.3`).

| Symptom | Cause and fix |
| --- | --- |
| `account '…' needs a login` | Apple ended the trusted session. Sign in again in the Apple ID list of the app or with `hw login`. |
| `Permission denied` on a NAS folder | Grant the user `sc-homeward` read/write access to the shared folder. |
| Run stopped by the delete brake | Check with a test run, then *Approve deletions* (or `--allow-deletes`). |
| Package says it is incompatible | Your model has no SynoCommunity Python 3.12 build. |
| Sign-in fails although the password is right | Advanced Data Protection is on, or Apple temporarily blocks web sign-in; try again later. |
| 421 "Invalid global session" right after signing in again | The account blocks web access to iCloud data; see [Known issues](#known-issues). |

## Known issues

### Photos backup fails with 421 "Invalid global session" right after signing in

The run stops with

```
account '…' needs a login: run `homeward login --account …` (Photos service not
available (Authentication required for Account. (421): {"reason":"Invalid global session","error":2}))
```

and the error comes back even after *Sign in again* with password and code succeeded.

**Cause:** the sign-in works, but the Apple account does not let web clients read its
iCloud data. Homeward reads iCloud the way icloud.com does, so it is refused as well.
This happens when *Advanced Data Protection* is on or *Access iCloud Data on the Web*
is off. Right after turning Advanced Data Protection off, Apple can take from a few
minutes to some hours until the data is reachable on the web again.

**Fix:**

1. On an iPhone signed in to that Apple ID, open *Settings › [your name] › iCloud*.
   Turn off *Advanced Data Protection* and turn on *Access iCloud Data on the Web*.
2. Sign in at [icloud.com/photos](https://www.icloud.com/photos) in a browser with the
   same Apple ID. Wait until the photos show there without asking for further approval.
3. In Homeward, open the Apple ID and choose *Sign in again*, then start the backup.
   Sync pairs, photo backups and their progress stay as they are.

If icloud.com shows the photos and Homeward still reports 421, open an issue with the
log excerpt.

When you open an issue, attach the relevant part of `homeward.log` and remove Apple IDs,
names and paths you do not want to share.

### Photos backup shows "Waiting for iCloud"

Apple answers "not finished indexing" while it rebuilds the index of a photo library,
for example after many photos were added or edited on a device. Homeward then waits up
to 3 minutes within the run and otherwise tries again 15 minutes later; the log names
the library and, when Apple reports it, how far the indexing is ("the Shared Library
(73 %)"). Only the libraries the backup includes count: indexing of another library is
logged and skipped.
