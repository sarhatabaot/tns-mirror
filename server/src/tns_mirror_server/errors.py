"""Error types shared across ports and adapters."""

from __future__ import annotations


class TnsMirrorError(Exception):
    """Base class for every error this service raises deliberately."""


class ConfigError(TnsMirrorError):
    """Configuration is missing, malformed, or internally inconsistent."""


class AuthNotConfigured(ConfigError):
    """No usable TNS credential.

    Invariant 4: the mirror is authenticated to TNS or it does not download.
    There is no anonymous fallback.
    """


class SourceError(TnsMirrorError):
    """The remote catalogue could not be fetched or decoded."""


class ParseError(SourceError, ValueError):
    """A catalogue file could not be parsed.

    Subclasses :class:`ValueError` because the engine treats an unparseable
    *delta* as a quiet hour rather than a failure, and that tolerance is
    expressed by catching ``ValueError`` (design A.2).
    """


class MigrationError(TnsMirrorError):
    """A migration is missing, out of order, or was edited after being applied."""
