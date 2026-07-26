"""Errors this library raises deliberately."""

from __future__ import annotations

__all__ = ["MirrorUnavailable", "SchemaVersionError", "TnsMirrorClientError"]


class TnsMirrorClientError(Exception):
    """Base class for every error this library raises on purpose."""


class SchemaVersionError(TnsMirrorClientError):
    """The mirror speaks a different major version of the contract.

    Client 1.x speaks schema v1. A mismatch means a column this library expects
    may have been renamed, dropped, or changed units — so failing loudly at
    connect time is better than returning quietly wrong answers later.
    """

    def __init__(self, found: int, expected: int) -> None:
        self.found = found
        self.expected = expected
        super().__init__(
            f"this mirror publishes schema v{found}, but tns-mirror-client "
            f"{expected}.x speaks schema v{expected}. Install a matching client: "
            f"pip install 'tns-mirror-client>={found},<{found + 1}'"
        )


class MirrorUnavailable(TnsMirrorClientError):
    """The mirror could not be reached, or is not a tns-mirror database."""
