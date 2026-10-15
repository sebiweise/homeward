"""Recognise Apple's common error messages, so the DSM UI can explain them in words.

pyicloud and Apple word their errors in English and not always helpfully
("Authentication required for Account. (421)"). ``classify`` maps the ones users
run into to a stable code; the UI has a text for each (``[apple]`` section).
"""

import re

_PATTERNS: tuple[tuple[str, str], ...] = (
    ("wrong_password", r"invalid email/password|-20101\b"),
    ("account_locked", r"apple id (?:has been|is) locked|-20209\b|account is locked"),
    ("code_rejected", r"2fa code was rejected|invalid verification code|-21669\b"),
    # Web access to iCloud data is off, or Advanced Data Protection is on.
    ("web_access", r"invalid global session|\(421\)|\b421\b.*session|session.*\b421\b"
                   r"|advanced data protection|access icloud data on the web"),
    ("rate_limited", r"\(429\)|http 429|too many requests|rate.?limit"),
    ("apple_unavailable", r"\(50[0234]\)|http 50[0234]|service unavailable|bad gateway"),
    ("network", r"timed out|connection (?:reset|refused|aborted)|name resolution"
                r"|max retries exceeded|network is unreachable|temporary failure in name"),
)  # fmt: skip
_COMPILED = tuple((code, re.compile(pattern, re.IGNORECASE)) for code, pattern in _PATTERNS)


def classify(message: str) -> str | None:
    """The code of the first known Apple error in ``message``, or None."""
    for code, pattern in _COMPILED:
        if pattern.search(message):
            return code
    return None
