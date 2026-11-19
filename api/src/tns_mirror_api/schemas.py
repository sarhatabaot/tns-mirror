"""The HTTP response shapes.

Deliberately *not* the client's dataclasses. Those track the SQL contract; these
are the wire contract, and the two are allowed to move independently. Returning
the client's model directly would make an internal rename of a Python field a
breaking change for every HTTP consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from tns_mirror_client import MirrorMeta, TnsObject

__all__ = ["ConeOut", "MetaOut", "ObjectOut", "ObjectsOut"]


@dataclass(frozen=True, slots=True)
class ObjectOut:
    """One TNS object, as JSON."""

    objid: int
    name: str
    ra: float
    dec: float
    type: str | None
    redshift: float | None
    discoverydate: datetime | None
    discoverymag: float | None
    internal_names: list[str]
    #: ``internal_names`` exactly as TNS publishes it, comma-separated, or
    #: null. The list above is for people; this is for a client that must
    #: return the stored value unchanged, as tns-mirror-client does.
    internal_names_raw: str | None
    reporting_group: str | None
    source_snapshot: date
    refreshed_at: datetime
    #: Present only on cone queries; there is no position to be separated from
    #: on a lookup by name or id.
    separation_arcsec: float | None = None

    @classmethod
    def of(cls, obj: TnsObject) -> ObjectOut:
        return cls(
            objid=obj.objid,
            name=obj.name,
            ra=obj.ra,
            dec=obj.dec,
            type=obj.type,
            redshift=obj.redshift,
            discoverydate=obj.discoverydate,
            discoverymag=obj.discoverymag,
            # A list rather than TNS's comma-separated string: every consumer
            # would otherwise split it themselves, and some would get it wrong.
            internal_names=obj.internal_name_list,
            internal_names_raw=obj.internal_names,
            reporting_group=obj.reporting_group,
            source_snapshot=obj.source_snapshot,
            refreshed_at=obj.refreshed_at,
            separation_arcsec=obj.separation_arcsec,
        )


@dataclass(frozen=True, slots=True)
class MetaOut:
    """Contract version and freshness — what a client should check before trusting a result."""

    schema_version: int
    rows: int
    last_full_sync_at: datetime | None
    last_delta_sync_at: datetime | None
    last_source_snapshot: date | None
    stale: bool

    @classmethod
    def of(cls, meta: MirrorMeta, rows: int, max_age_hours: float) -> MetaOut:
        return cls(
            schema_version=meta.schema_version,
            rows=rows,
            last_full_sync_at=meta.last_full_sync_at,
            last_delta_sync_at=meta.last_delta_sync_at,
            last_source_snapshot=meta.last_source_snapshot,
            stale=not meta.is_fresh(max_age_hours),
        )


@dataclass(frozen=True, slots=True)
class ConeOut:
    """A cone search result.

    Echoes the query back because the radius and the limit may have been
    clamped, and a caller comparing counts across requests needs to know what
    was actually searched.
    """

    ra: float
    dec: float
    radius_arcsec: float
    #: The most results this response could hold, after clamping.
    limit: int
    count: int
    #: More objects lie within the radius than ``results`` holds. Without it a
    #: caller cannot tell a complete answer from one cut off at ``limit``.
    truncated: bool
    results: list[ObjectOut]


@dataclass(frozen=True, slots=True)
class ObjectsOut:
    """A lookup of several names. Names with no match are simply absent."""

    count: int
    results: list[ObjectOut]
