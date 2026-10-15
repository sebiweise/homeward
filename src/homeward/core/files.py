"""Put finished files into place without ever replacing an existing file.

Files are written under a temporary name and then renamed with Linux'
``RENAME_NOREPLACE``. A rename shows up as a "moved in" event, which is what DSM's
indexers (Synology Photos, Media Server) react to. A hard link only shows up as
"created" without a finished write, so files hard-linked into place were never
indexed: Synology Photos showed the folders but no photos in them.

Where ``RENAME_NOREPLACE`` is unavailable (old kernel, file system without it) the
fallback is hard link + unlink, which is just as safe but not seen by the indexers.
"""

import ctypes
import errno
import os
import platform
import secrets
from collections.abc import Callable
from pathlib import Path

from homeward.core.rules import TEMP_SUFFIX

_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
# renameat2 syscall numbers for kernels whose C library lacks the wrapper (glibc < 2.28)
_SYSCALL_RENAMEAT2 = {"x86_64": 316, "aarch64": 276, "armv7l": 382, "armv8l": 382, "i686": 353}
_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}


def _load_renameat2() -> Callable[..., int] | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    func = getattr(libc, "renameat2", None)
    if func is not None:
        return lambda *args: int(func(*args))
    number = _SYSCALL_RENAMEAT2.get(platform.machine())
    syscall = getattr(libc, "syscall", None)
    if number is None or syscall is None:
        return None

    return lambda *args: int(syscall(number, *args))


_renameat2 = _load_renameat2()


def rename_noreplace(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target``; raise ``FileExistsError`` if ``target`` exists."""
    if _renameat2 is not None:
        result = _renameat2(
            _AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE
        )
        if result == 0:
            return
        code = ctypes.get_errno()
        if code not in _UNSUPPORTED:
            raise OSError(code, os.strerror(code), str(source), None, str(target))
    os.link(source, target)  # fails if target exists
    os.unlink(source)


def temp_name(target: Path) -> Path:
    """A hidden, unique name next to ``target`` that the sync and indexers skip."""
    return target.with_name(f".{target.name}.{secrets.token_hex(4)}{TEMP_SUFFIX}")


def link_noreplace(existing: Path, target: Path) -> None:
    """Hard-link ``existing`` to ``target`` (fails if it exists), visible to indexers."""
    tmp = temp_name(target)
    os.link(existing, tmp)
    try:
        rename_noreplace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
