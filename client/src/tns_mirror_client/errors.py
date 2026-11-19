"""Errors this library raises deliberately."""

from __future__ import annotations

__all__ = [
    "AccessDenied",
    "MirrorUnavailable",
    "QueryLimitExceeded",
    "SchemaVersionError",
    "TnsMirrorClientError",
]


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
    """The mirror could not be reached, or is not a tns-mirror database.

    Over HTTP this also covers a timeout, a 5xx, a rate limit (429), and a URL
    that does not answer like a tns-mirror API.
    """


class AccessDenied(TnsMirrorClientError):
    """The mirror's HTTP API refused the API key (HTTP 401 or 403).

    The message never contains the key. Retrying with the same key will not
    help; the key is wrong, revoked, or not sent where the operator expects it.
    """


class QueryLimitExceeded(TnsMirrorClientError):
    """The mirror's HTTP API would have answered with fewer results than exist.

    The API caps the radius and the number of results it serves in one query.
    Over SQL there is no cap, so rather than quietly return a partial answer
    over HTTP, the client raises this. Narrow the radius or pass a ``limit``.
    """
