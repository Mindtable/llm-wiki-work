"""Stable user-facing errors for the wiki CLI."""


class WikiError(Exception):
    """An expected wiki operation failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        if not code or not isinstance(code, str):
            raise ValueError("code must be a non-empty string")
        if not message or not isinstance(message, str):
            raise ValueError("message must be a non-empty string")
        self.code = code
        self.message = message
        super().__init__(message)
