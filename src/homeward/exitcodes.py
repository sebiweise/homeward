"""Exit codes of the ``homeward`` CLI, which the daemon turns into run results.

Documented in docs/architecture.md.
"""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2  # also an invalid config
EXIT_BLOCKED = 3  # the delete brake stopped the run
EXIT_WAITING = 4  # iCloud is not ready yet (e.g. still indexing); the next run tries again
EXIT_CANCELLED = 5  # ended on SIGTERM (cancelled, or the service stops)
EXIT_LOGIN = 6  # the Apple ID needs a new sign-in (the daemon notifies the admins)
