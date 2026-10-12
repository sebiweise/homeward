"""Errors the API reports to the UI."""


class ApiError(Exception):
    """A failed request. ``code`` is stable and maps to a UI string; ``message`` is detail."""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status
