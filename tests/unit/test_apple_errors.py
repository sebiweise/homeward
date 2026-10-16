import pytest

from homeward.apple_errors import classify


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Invalid email/password combination.", "wrong_password"),
        ("Service error -20101", "wrong_password"),
        ("This Apple ID has been locked for security reasons.", "account_locked"),
        ("the 2FA code was rejected", "code_rejected"),
        ("Invalid global session", "web_access"),
        ("Authentication required for Account. (421)", "web_access"),
        ("Turn on Access iCloud Data on the Web", "web_access"),
        ("Too Many Requests (429)", "rate_limited"),
        ("Service Unavailable (503)", "apple_unavailable"),
        ("Read timed out. (read timeout=30)", "network"),
        ("Max retries exceeded with url: /setup", "network"),
        ("b.txt: 503", None),
        ("", None),
    ],
)
def test_classify(message: str, code: str | None) -> None:
    assert classify(message) == code
