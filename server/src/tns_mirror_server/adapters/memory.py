"""In-memory fakes for the ports.

Design §3: every port has an in-memory fake so the engine's behaviour — atomic
replace, idempotent delta, catch-up ordering, rate-limit backoff — is tested
without a database or a network.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from ..errors import ParseError
from ..ports.source import TnsRecord
from ..ports.store import MirrorMeta

__all__ = ["FakeSource", "MemoryStore"]


class MemoryStore:
    """A ``Store`` that keeps rows in a dict, keyed by ``objid``."""

    def __init__(self, *, schema_version: int = 1) -> None:
        self.rows: dict[int, TnsRecord] = {}
        self.snapshots: dict[int, date] = {}
        self.replace_calls = 0
        self.upsert_calls = 0
        self.migrated = False
        self._schema_version = schema_version
        self._last_full: datetime | None = None
        self._last_delta: datetime | None = None
        self._last_snapshot: date | None = None

    def migrate(self) -> list[str]:
        already, self.migrated = self.migrated, True
        return [] if already else ["0001_initial"]

    def replace(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        # Materialise before mutating: a real transaction would not expose a
        # partially-applied swap either, and tests should not be able to observe
        # an intermediate state the Postgres store cannot produce.
        incoming = {record.objid: record for record in records if record.objid is not None}
        self.rows = incoming
        self.snapshots = dict.fromkeys(incoming, snapshot)
        self.replace_calls += 1
        self._last_full = datetime.now(UTC)
        self._last_snapshot = snapshot
        return len(incoming)

    def upsert(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        written = 0
        for record in records:
            if record.objid is None:
                continue
            self.rows[record.objid] = record
            self.snapshots[record.objid] = snapshot
            written += 1
        self.upsert_calls += 1
        if written:
            self._last_delta = datetime.now(UTC)
        return written

    def count(self) -> int:
        return len(self.rows)

    def meta(self) -> MirrorMeta:
        return MirrorMeta(
            schema_version=self._schema_version,
            last_full_sync_at=self._last_full,
            last_delta_sync_at=self._last_delta,
            last_source_snapshot=self._last_snapshot,
        )

    # convenience for assertions
    def names(self) -> set[str]:
        return {record.name for record in self.rows.values()}


class FakeSource:
    """A ``Source`` serving canned records, recording what was asked for.

    ``full`` is returned for the snapshot; ``deltas`` maps hour -> records. An
    hour that is absent behaves like a quiet hour (raises ``ParseError``, which
    the engine tolerates); an hour mapped to an exception raises it, which is how
    the 429 backoff is tested.
    """

    def __init__(
        self,
        full: list[TnsRecord] | None = None,
        deltas: dict[int, list[TnsRecord] | Exception] | None = None,
        *,
        throttle: float = 0.0,
    ) -> None:
        self.full = full or []
        self.deltas = deltas or {}
        self.download_throttle_seconds = throttle
        self.downloaded: list[int | None] = []
        self._pending: list[TnsRecord] | Exception | None = None

    def download(self, hour: int | None = None, *, reuse_existing: bool = False) -> Path:
        self.downloaded.append(hour)
        payload = self.full if hour is None else self.deltas.get(hour)
        if isinstance(payload, Exception):
            raise payload
        self._pending = payload
        return Path(f"/fake/tns_public_objects{'' if hour is None else f'_{hour:02d}'}.csv")

    def parse(self, source: Path) -> Iterator[TnsRecord]:
        payload = self._pending
        if payload is None:
            raise ParseError(f"quiet hour: no header row in {source}")
        yield from payload
