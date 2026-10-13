"""DSM desktop notifications through ``synodsmnotify``.

Title and message are string keys (``homeward:section:key``) that DSM looks up in
the package's ``ui/texts/<lang>/strings``, so each administrator reads them in
their own DSM language. Extra arguments replace ``{0}``, ``{1}`` ... in the text.
"""

import logging
import shutil
import subprocess
from enum import StrEnum

log = logging.getLogger(__name__)

SYNODSMNOTIFY = "/usr/syno/bin/synodsmnotify"
APP_CLASS = "SYNO.SDS.Homeward.Instance"
RECIPIENTS = "@administrators"
TITLE_KEY = "homeward:app:app_name"


class Event(StrEnum):
    """Each value is a key in the ``[notify]`` section of the UI strings; the
    string takes one argument."""

    SYNC_FAILED = "sync_failed"  # names the failed pair or photos backup
    DELETE_BRAKE = "delete_brake"  # names the pair
    LOGIN_NEEDED = "login_needed"  # names the account
    CONFLICTS = "conflicts"  # names the pair


def notify(event: Event, *args: str, binary: str = SYNODSMNOTIFY) -> bool:
    """Send a notification to all administrators. Never raises; returns success."""
    if shutil.which(binary) is None:
        log.info("notification %s not sent: %s not found", event, binary)
        return False
    command = [binary, "-c", APP_CLASS, RECIPIENTS, TITLE_KEY, f"homeward:notify:{event}", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("notification %s failed: %s", event, exc)
        return False
    if result.returncode != 0:
        log.warning("notification %s failed: %s", event, result.stderr.strip())
        return False
    log.info("notification %s sent for %s", event, ", ".join(args) or "-")
    return True
