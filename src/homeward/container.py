"""Entry point for running Homeward in a container instead of as a DSM package.

Writes a default config on the first start, brings an older config up to date, starts
the web page when a password is set and then runs the daemon in the foreground.

Settings come from the environment:

- ``HOMEWARD_VAR`` / ``HOMEWARD_CONFIG``: config, state and logs (see ``runtime``)
- ``HOMEWARD_WEB_PASSWORD``: turns the web page on; without it Homeward runs
  headless from ``config.toml`` alone
- ``HOMEWARD_WEB_USER`` (default ``admin``), ``HOMEWARD_WEB_HOST`` (``0.0.0.0``),
  ``HOMEWARD_WEB_PORT`` (``8080``), ``HOMEWARD_UI_DIR`` (``/app/ui``)
- ``HOMEWARD_VOLUMES``: the folders whose subfolders the folder picker offers
"""

import logging
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from homeward.api.accounts import default_backend
from homeward.api.store import ConfigStore
from homeward.daemon import scheduler
from homeward.runtime import Paths
from homeward.web import Credentials, make_server

log = logging.getLogger("homeward.container")

DEFAULT_CONFIG = """\
# Homeward configuration. Add sync pairs and photo backups here or on the web page.
# See docs/architecture.md for all options and docs/docker.md for the container.
state_dir = "state"

[daemon]
interval_minutes = 60

# [[pair]]
# name = "docs"
# local = "/data/documents"
# remote = "Documents"
# mode = "upload"
"""


def ensure_config(paths: Paths) -> bool:
    """Write the default config if there is none yet; True if it was written."""
    if paths.config.exists():
        return False
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return True


def start_web(paths: Paths, environ: Mapping[str, str]) -> threading.Thread | None:
    """Serve the web page in a background thread, if ``HOMEWARD_WEB_PASSWORD`` is set."""
    password = environ.get("HOMEWARD_WEB_PASSWORD", "")
    if not password:
        log.info("web page off (HOMEWARD_WEB_PASSWORD is not set), running from the config file")
        return None
    host = environ.get("HOMEWARD_WEB_HOST", "0.0.0.0")
    port = int(environ.get("HOMEWARD_WEB_PORT", "8080"))
    ui_dir = Path(environ.get("HOMEWARD_UI_DIR", "/app/ui"))
    credentials = Credentials(environ.get("HOMEWARD_WEB_USER", "admin"), password)
    server = make_server(host, port, ui_dir, paths, credentials, lambda: default_backend(paths))
    thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    thread.start()
    log.info("web page on http://%s:%d/ (user %s)", host, port, credentials.user)
    return thread


def main(argv: Sequence[str] | None = None) -> int:
    """Extra arguments (``--once``) go to the daemon."""
    extra = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    paths = Paths.from_env()
    if ensure_config(paths):
        log.info("wrote the default configuration to %s", paths.config)
    for name in ConfigStore(paths).migrate():
        log.info("pair %s: set beta = true (bidirectional sync is in beta)", name)
    start_web(paths, os.environ)
    return scheduler.main(["--config", str(paths.config), *extra])


if __name__ == "__main__":
    raise SystemExit(main())
