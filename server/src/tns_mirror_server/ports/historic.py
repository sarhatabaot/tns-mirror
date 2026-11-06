"""The ``HistoricSource`` port: where the pre-2016 supernovae come from.

TNS imported the CBAT-era SNe as a closed list and exposes them only through its
search page, not the staged csv. The shape is different enough — sexagesimal
coordinates, a different column set — that they get their own record type and
their own table, rather than being squeezed into :class:`TnsRecord`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "HistoricRecord",
    "HistoricSource",
    "get_historic_source",
    "register_historic_source",
]


@dataclass(frozen=True, slots=True)
class HistoricRecord:
    """One historic SN, named exactly as ``tns_historic_sne`` names its columns.

    Everything TNS exports is carried as the string it exported (``None`` for an
    empty cell). The only interpreted fields are the four the table needs to be
    useful: ``objid`` (the primary key), ``name`` (whitespace removed so it
    matches ``tns_objects.name``), and ``ra``/``dec`` in degrees so the cone
    search prefilter index can exist. The original coordinate strings travel
    alongside in ``ra_hms``/``dec_dms``.
    """

    objid: int | None
    name: str
    ra: float
    dec: float
    ra_hms: str
    dec_dms: str
    obj_type: str | None = None
    redshift: str | None = None
    host_name: str | None = None
    host_redshift: str | None = None
    reporting_groups: str | None = None
    discovery_data_sources: str | None = None
    classifying_groups: str | None = None
    associated_groups: str | None = None
    disc_internal_name: str | None = None
    disc_instruments: str | None = None
    class_instruments: str | None = None
    tns_at: str | None = None
    public: str | None = None
    end_prop_period: str | None = None
    discovery_mag_flux: str | None = None
    discovery_filter: str | None = None
    discovery_date_ut: str | None = None
    sender: str | None = None
    remarks: str | None = None
    unreal: str | None = None
    discovery_bibcode: str | None = None
    classification_bibcodes: str | None = None
    ext_catalogs: str | None = None
    auto_classification: str | None = None

    def as_row(self) -> dict[str, object]:
        """Render as a column-name -> value mapping for the store."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@runtime_checkable
class HistoricSource(Protocol):
    """A publisher of the historic SN list.

    ``fetch`` pulls the whole list — every page of TNS's search export — into
    one local file and returns its path; ``parse`` turns that file into records.
    The split keeps network I/O outside the database transaction, exactly as
    :class:`~tns_mirror_server.ports.source.Source` does.
    """

    def fetch(self, *, reuse_existing: bool = False) -> Path:
        """Download the complete historic list and return its local path.

        Raises :class:`~tns_mirror_server.errors.AuthNotConfigured` when no TNS
        credential is configured — never falls back to an anonymous request.
        """
        ...

    def parse(self, source: Path) -> Iterator[HistoricRecord]:
        """Yield records, skipping rows that are unusable.

        Raises :class:`~tns_mirror_server.errors.ParseError` when the file has
        no locatable header row.
        """
        ...


# --- registry ---------------------------------------------------------------
#
# Keyed by the same provider name as the ``Source`` registry ("tns"), so one
# ``source:`` setting selects both of a provider's feeds.

_HISTORIC_SOURCES: dict[str, Callable[..., HistoricSource]] = {}


def register_historic_source(name: str) -> Callable[[type], type]:
    """Class decorator registering a :class:`HistoricSource` under provider ``name``."""

    def decorate(cls: type) -> type:
        if name in _HISTORIC_SOURCES:
            raise ValueError(f"historic source already registered: {name!r}")
        _HISTORIC_SOURCES[name] = cls
        return cls

    return decorate


def get_historic_source(name: str) -> Callable[..., HistoricSource]:
    """Look up the historic feed for provider ``name``, or raise ``KeyError``."""
    try:
        return _HISTORIC_SOURCES[name]
    except KeyError:
        raise KeyError(
            f"provider {name!r} has no historic source; known: {sorted(_HISTORIC_SOURCES)}"
        ) from None
