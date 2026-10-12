#!/bin/sh
# JSON API for the Homeward page. It must run as the package user, like the daemon,
# so both can read the config, the state and the Apple ID sessions.
HOMEWARD_VAR=/var/packages/homeward/var
HOMEWARD_CONFIG="${HOMEWARD_VAR}/config.toml"
HOME="${HOMEWARD_VAR}"
export HOMEWARD_VAR HOMEWARD_CONFIG HOME
exec /var/packages/homeward/target/env/bin/python3 -m homeward.api.cgi
