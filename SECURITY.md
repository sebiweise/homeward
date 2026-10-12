# Security policy

Homeward stores trusted iCloud sessions on your NAS and moves your files, so security
reports are very welcome.

## Supported versions

Only the latest release receives fixes while Homeward is below 1.0.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/sebiweise/homeward/security/advisories/new)
with:

- what an attacker can do and under which conditions,
- steps to reproduce or a proof of concept,
- the Homeward and DSM versions.

You will get an answer within 7 days. Once a fix is released the advisory is published
and you are credited unless you prefer otherwise.

## Scope

In scope: the Homeward package, its CLI, daemon, DSM app and build tooling in this
repository. Out of scope: weaknesses in iCloud, DSM or pyicloud themselves; report those
to their maintainers.

## Handling your own data

- Homeward never stores your Apple ID password, only the trusted session in
  `/var/packages/homeward/var/state/sessions/`. Treat that folder like a password.
- Remove Apple IDs, names and paths from logs before sharing them.
