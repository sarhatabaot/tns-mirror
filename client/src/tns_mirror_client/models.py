"""Typed results — the schema contract expressed as Python.

Field names track the column names in ``schema_v1.sql`` exactly. That is not a
coincidence to be tidied up later: the schema is the contract, and a client that
renames its columns has invented a second one. A contract test asserts the two
stay in step.

Plain dataclasses, not pydantic: this is a read client that consumers embed in
their own applications, and every dependency it carries becomes theirs.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta

__all__ = ["CATALOGUE_COLUMNS", "MirrorMeta", "TnsObject"]


@dataclass(frozen=True, slots=True)
class TnsObject:
    """One row of ``tns_objects``.

    Everything but ``objid``, ``name``, ``ra``, ``dec``, ``source_snapshot`` and
    ``refreshed_at`` is nullable, and a minimal TNS row is common — do not assume
    ``type`` or ``redshift`` is populated.
    """

    objid: int
    name: str
    ra: float
    dec: float
    type: str | None
    redshift: float | None
    discoverydate: datetime | None
    discoverymag: float | None
    internal_names: str | None
    reporting_group: str | None
    source_snapshot: date
    refreshed_at: datetime

    #: Angular distance from the query position. Populated only by cone queries
    #: (:meth:`~tns_mirror_client.TnsMirror.nearest` and ``search``); ``None``
    #: everywhere else, because there is no position to be separated from.
    separation_arcsec: float | None = None

    @property
    def internal_name_list(self) -> list[str]:
        """``internal_names`` split into individual survey identifiers."""
        if not self.internal_names:
            return []
        return [part.strip() for part in self.internal_names.split(",") if part.strip()]

    @property
    def is_classified(self) -> bool:
        """Whether TNS has published a spectroscopic classification."""
        return bool(self.type)


#: The catalogue columns, in contract order. Excludes `separation_arcsec`, which
#: is derived per query rather than stored.
CATALOGUE_COLUMNS: tuple[str, ...] = tuple(
    field.name for field in fields(TnsObject) if field.name != "separation_arcsec"
)


@dataclass(frozen=True, slots=True)
class MirrorMeta:
    """Contents of ``tns_mirror_meta`` — the contract version and how fresh the data is."""

    schema_version: int
    last_full_sync_at: datetime | None
    last_delta_sync_at: datetime | None
    last_source_snapshot: date | None

    @property
    def last_sync_at(self) -> datetime | None:
        """The more recent of the two syncs, or ``None`` if neither has run."""
        candidates = [t for t in (self.last_full_sync_at, self.last_delta_sync_at) if t]
        return max(candidates) if candidates else None

    @property
    def age(self) -> timedelta | None:
        """How long since anything last synced, or ``None`` if nothing ever has."""
        last = self.last_sync_at
        return None if last is None else datetime.now(UTC) - last

    def is_fresh(self, max_age_hours: float = 26.0) -> bool:
        """Whether the mirror synced recently enough to trust.

        Worth checking before a batch job: a row count alone looks identical
        whether the mirror synced an hour ago or died three weeks ago. The
        default allows a missed daily snapshot plus a couple of hours' slack.
        """
        age = self.age
        return age is not None and age <= timedelta(hours=max_age_hours)
