"""The ``Source`` port: where catalogue rows come from.

Everything that touches the network sits behind this protocol so the sync engine
is both auth-agnostic (design §5.0) and network-free in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["Source", "TnsRecord", "get_source", "known_sources", "register_source"]


@dataclass(frozen=True, slots=True)
class TnsRecord:
    """One catalogue row, named exactly as the schema names it.

    Field names track the column names in ``schema/schema_v1.sql`` deliberately:
    the schema is the contract (invariant 1), so the record that feeds it carries
    no other vocabulary. In particular there is no ``ext_id``/``ext_name``
    generic-catalogue naming here — that belongs to a consumer's model, and the
    mirror knows nothing about its consumers (invariant 2).

    ``objid`` is optional at the port boundary because TNS files do occasionally
    carry rows without one; the engine drops them, since without a stable id a
    row can be neither addressed nor upserted.
    """

    objid: int | None
    name: str
    ra: float
    dec: float
    type: str | None = None
    redshift: float | None = None
    discoverydate: datetime | None = None
    discoverymag: float | None = None
    internal_names: str | None = None
    reporting_group: str | None = None

    def as_row(self) -> dict[str, object]:
        """Render as a column-name -> value mapping for the store."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@runtime_checkable
class Source(Protocol):
    """A bulk catalogue publisher.

    ``download`` fetches one file — the full snapshot when ``hour`` is ``None``,
    otherwise that hour's delta — and returns a local path. ``parse`` turns that
    path into records. Splitting them keeps network I/O outside the database
    transaction (design A.2).
    """

    #: Seconds to wait between consecutive downloads, to stay under TNS's rate limit.
    download_throttle_seconds: float

    def download(self, hour: int | None = None, *, reuse_existing: bool = False) -> Path:
        """Fetch the catalogue file and return its local path.

        Raises :class:`~tns_mirror_server.errors.AuthNotConfigured` when no TNS
        credential is configured — never falls back to an anonymous request.
        """
        ...

    def parse(self, source: Path) -> Iterator[TnsRecord]:
        """Yield records, skipping rows that are unusable.

        Raises :class:`~tns_mirror_server.errors.ParseError` (a ``ValueError``)
        when the file has no locatable header row.
        """
        ...


# --- registry ---------------------------------------------------------------
#
# TNS is the only shipped source, but the engine is provider-agnostic, so the
# registry keeps the door open for another bulk-CSV catalogue without touching
# the engine (design §3).

_SOURCES: dict[str, Callable[..., Source]] = {}


def register_source(name: str) -> Callable[[type], type]:
    """Class decorator registering a :class:`Source` implementation under ``name``."""

    def decorate(cls: type) -> type:
        if name in _SOURCES:
            raise ValueError(f"source already registered: {name!r}")
        _SOURCES[name] = cls
        return cls

    return decorate


def get_source(name: str) -> Callable[..., Source]:
    """Look up a registered source factory, or raise ``KeyError`` listing what exists."""
    try:
        return _SOURCES[name]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; known: {sorted(_SOURCES)}") from None


def known_sources() -> list[str]:
    return sorted(_SOURCES)
