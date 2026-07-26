"""The sync engine: provider-agnostic, no I/O of its own.

Design §5 and A.2. Everything here is expressed against the ``Source`` and
``Store`` ports, so the whole engine is exercised in tests with an in-memory
store and a fake source — no network, no database.

Deliberately *not* here: cone search, cross-matching, or anything that decides
what a TNS name means. The server keeps the catalogue current; interpreting it is
the client's job (design §10, and "What does NOT come across" in A.6).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import requests

from .ports.source import Source, TnsRecord
from .ports.store import Store

log = logging.getLogger(__name__)

__all__ = ["CatchUpResult", "SyncResult", "catch_up", "delta_sync", "full_sync"]


@dataclass(frozen=True, slots=True)
class SyncResult:
    kind: str
    snapshot: date
    rows_written: int
    rows_skipped: int
    hour: int | None = None

    def describe(self) -> str:
        where = "full snapshot" if self.hour is None else f"delta hour {self.hour:02d}"
        skipped = f", {self.rows_skipped} skipped" if self.rows_skipped else ""
        return f"{where} ({self.snapshot}): {self.rows_written} rows written{skipped}"


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    hours: list[int]
    results: list[SyncResult] = field(default_factory=list)
    stopped_early: bool = False

    @property
    def rows_written(self) -> int:
        return sum(result.rows_written for result in self.results)

    def describe(self) -> str:
        applied = "/".join(f"{r.hour:02d}" for r in self.results) or "none"
        tail = " (stopped early on HTTP 429)" if self.stopped_early else ""
        return (
            f"catch-up over {len(self.hours)}h: applied {applied}, "
            f"{self.rows_written} rows written{tail}"
        )


class _Skipped:
    """Counts records dropped for having no stable id, without buffering the stream."""

    def __init__(self) -> None:
        self.count = 0


def _addressable(records: Iterable[TnsRecord], skipped: _Skipped) -> Iterator[TnsRecord]:
    """Drop rows with no ``objid``.

    ``objid`` is the primary key and the upsert conflict target, so a row without
    one can be neither stored nor updated. TNS ships very few of these; dropping
    them keeps a single malformed line from aborting an entire refresh, which
    matters because the full snapshot commits as one transaction.
    """
    for record in records:
        if record.objid is None:
            skipped.count += 1
            continue
        yield record


def _utcnow() -> datetime:
    return datetime.now(UTC)


def snapshot_for_hour(hour: int, now: datetime | None = None) -> date:
    """The UTC date the file for ``hour`` belongs to.

    A catch-up window run at 01:00 covers hours ``[23, 00, 01]``, and hour 23 is
    *yesterday's* file. Getting this wrong would stamp ``source_snapshot`` a day
    ahead on every wrapped window.
    """
    now = now or _utcnow()
    return now.date() if hour <= now.hour else now.date() - timedelta(days=1)


def full_sync(store: Store, source: Source, *, reuse_existing: bool = False) -> SyncResult:
    """Download the daily snapshot and swap the catalogue for it.

    Strict by design: a file we cannot parse raises rather than silently
    replacing a good catalogue with an empty one.
    """
    snapshot = _utcnow().date()
    path = source.download(reuse_existing=reuse_existing)  # network OUTSIDE the txn
    skipped = _Skipped()
    written = store.replace(_addressable(source.parse(path), skipped), snapshot=snapshot)
    result = SyncResult("full", snapshot, written, skipped.count)
    log.info("%s", result.describe())
    return result


def delta_sync(
    store: Store,
    source: Source,
    hour: int,
    *,
    now: datetime | None = None,
    reuse_existing: bool = False,
) -> SyncResult:
    """Apply one hourly delta. Tolerant: a quiet hour is a no-op, not a failure."""
    snapshot = snapshot_for_hour(hour, now)
    path = source.download(hour=hour, reuse_existing=reuse_existing)
    skipped = _Skipped()
    try:
        records = list(_addressable(source.parse(path), skipped))
    except ValueError as exc:
        # No header row: TNS publishes an empty file for an hour with no edits.
        log.info("delta hour %02d is quiet (%s)", hour, exc)
        return SyncResult("delta", snapshot, 0, skipped.count, hour=hour)

    written = store.upsert(records, snapshot=snapshot)
    result = SyncResult("delta", snapshot, written, skipped.count, hour=hour)
    log.info("%s", result.describe())
    return result


def catch_up(
    store: Store,
    source: Source,
    hours: int = 24,
    *,
    end_hour: int | None = None,
    now: datetime | None = None,
) -> CatchUpResult:
    """Apply a trailing window of hourly deltas, oldest first.

    Oldest-to-newest matters: an object edited in several hours of the window
    must land at its *latest* state, and the deltas are applied in file order.

    Stops early on HTTP 429 rather than hammering a rate-limiting TNS. Because
    each hour commits on its own, an interrupted catch-up keeps everything it
    already applied, and re-running simply continues.
    """
    now = now or _utcnow()
    if end_hour is None:
        end_hour = now.hour
    hours = max(1, min(24, hours))

    labels = [(end_hour - offset) % 24 for offset in reversed(range(hours))]
    throttle = getattr(source, "download_throttle_seconds", 0.0)

    results: list[SyncResult] = []
    stopped_early = False
    for index, hour in enumerate(labels):
        if index and throttle:
            time.sleep(throttle)  # stay under TNS's rate limit
        try:
            results.append(delta_sync(store, source, hour, now=now))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                log.warning(
                    "TNS rate-limited us at hour %02d; stopping catch-up early. "
                    "Already-applied hours are committed and the next run resumes.",
                    hour,
                )
                stopped_early = True
                break
            raise

    result = CatchUpResult(hours=labels, results=results, stopped_early=stopped_early)
    log.info("%s", result.describe())
    return result
