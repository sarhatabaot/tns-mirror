"""The ``Store`` port: the only place that knows SQL or the table name.

Design §3. An in-memory fake implements this same protocol so the engine's tests
need no database.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from .historic import HistoricRecord
from .source import TnsRecord

__all__ = ["MirrorMeta", "Store"]


@dataclass(frozen=True, slots=True)
class MirrorMeta:
    """Contents of the ``tns_mirror_meta`` row — contract version and freshness."""

    schema_version: int
    last_full_sync_at: datetime | None
    last_delta_sync_at: datetime | None
    last_source_snapshot: date | None
    last_historic_sync_at: datetime | None = None


@runtime_checkable
class Store(Protocol):
    """Persistence for catalogue rows.

    Three write paths, mirroring how TNS publishes:

    * :meth:`replace` — the daily full snapshot, a delete-then-insert inside one
      transaction so a reader never sees a half-written catalogue and
      de-published objects actually disappear (invariant 3).
    * :meth:`upsert` — an hourly delta, keyed on ``objid``, idempotent.
    * :meth:`replace_historic` — the pre-2016 SN list, into its own table, with
      the same delete-then-insert-in-one-transaction discipline as
      :meth:`replace`. The two tables are written independently: a full
      snapshot never touches the historic rows, and vice versa.

    All stamp ``source_snapshot`` and ``refreshed_at``, and all update the
    freshness columns of ``tns_mirror_meta`` in the *same* transaction as the
    data, so freshness can never claim a sync that did not commit.
    """

    def migrate(self) -> list[str]:
        """Apply pending migrations; return the versions applied, oldest first."""
        ...

    def replace(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        """Atomically swap the catalogue for ``records``. Returns rows written."""
        ...

    def upsert(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        """Merge ``records`` on ``objid``. Returns rows written."""
        ...

    def replace_historic(self, records: Iterable[HistoricRecord], *, snapshot: date) -> int:
        """Atomically swap the historic SN table for ``records``. Returns rows written."""
        ...

    def count(self) -> int:
        """Number of rows currently in the catalogue."""
        ...

    def count_historic(self) -> int:
        """Number of rows currently in the historic SN table."""
        ...

    def meta(self) -> MirrorMeta:
        """Schema version and sync freshness."""
        ...
