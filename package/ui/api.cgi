#!/bin/sh
# JSON API for the Homeward page. DSM runs 3rdparty CGIs as the package user.
PYTHON=/var/packages/homeward/target/env/bin/python3
HOMEWARD_VAR=/var/packages/homeward/var
export HOMEWARD_VAR
exec "$PYTHON" -m homeward.api.cgi
