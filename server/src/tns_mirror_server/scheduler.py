"""A small UTC cron scheduler for the ``serve`` command.

Design §5.4. One-shot commands are the primitive; this is the thin wrapper that
turns them into a long-lived process for ``docker compose up``. Kubernetes
deployments run CronJobs against the one-shot commands and never load this.

Cron parsing is implemented here rather than taken as a dependency: the subset
needed is small, and §8 asks for a small dependency tree because every dependency
is attack surface in a published image.

Schedules are evaluated in **UTC**, because TNS stages its hourly files on UT
hours and a local-time schedule would drift off them twice a year.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

__all__ = ["CronSpec", "Job", "Scheduler"]

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


def _parse_field(spec: str, low: int, high: int, name: str) -> tuple[frozenset[int], bool]:
    """Parse one cron field into its matching values, and whether it is ``*``."""
    values: set[int] = set()
    unrestricted = spec.strip() == "*"

    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty {name} field in cron expression")

        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ValueError(f"invalid step {raw_step!r} in {name} field")
            step = int(raw_step)

        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            raw_start, _, raw_end = part.partition("-")
            start, end = _as_int(raw_start, name), _as_int(raw_end, name)
        else:
            start = end = _as_int(part, name)

        # Sunday is both 0 and 7 in every cron implementation worth matching.
        if name == "day-of-week":
            start, end = start % 7, end % 7
            if end < start:
                start, end = end, start

        if not (low <= start <= high and low <= end <= high) or end < start:
            raise ValueError(f"{name} value out of range: {part!r} (expected {low}-{high})")
        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"{name} field matched nothing")
    return frozenset(values), unrestricted


def _as_int(raw: str, name: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError(f"invalid {name} value {raw!r}")
    return int(raw)


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A parsed five-field cron expression, evaluated in UTC."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_unrestricted: bool
    weekday_unrestricted: bool
    expression: str

    @classmethod
    def parse(cls, expression: str) -> CronSpec:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression must have 5 fields "
                f"(minute hour day-of-month month day-of-week), got {expression!r}"
            )
        parsed = [
            _parse_field(field, low, high, name)
            for field, (low, high), name in zip(
                fields, _FIELD_RANGES, _FIELD_NAMES, strict=True
            )
        ]
        return cls(
            minutes=parsed[0][0],
            hours=parsed[1][0],
            days=parsed[2][0],
            months=parsed[3][0],
            weekdays=parsed[4][0],
            day_unrestricted=parsed[2][1],
            weekday_unrestricted=parsed[4][1],
            expression=expression,
        )

    def _day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        # Standard cron: when both day-of-month and day-of-week are restricted
        # they are OR-ed, not AND-ed. Getting this backwards silently skips runs.
        dom_ok = moment.day in self.days
        dow_ok = moment.isoweekday() % 7 in self.weekdays
        if self.day_unrestricted and self.weekday_unrestricted:
            return True
        if self.day_unrestricted:
            return dow_ok
        if self.weekday_unrestricted:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime) -> datetime:
        """The first matching minute strictly after ``after`` (UTC)."""
        candidate = (after.astimezone(UTC) + timedelta(minutes=1)).replace(
            second=0, microsecond=0
        )
        limit = candidate + timedelta(days=1500)  # covers Feb-29-only expressions
        while candidate < limit:
            if not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue
            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
                continue
            return candidate
        raise ValueError(f"cron expression never fires: {self.expression!r}")


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    spec: CronSpec
    run: Callable[[], object]


class Scheduler:
    """Runs jobs on their cron schedules until asked to stop.

    A job that raises is logged and the loop continues (invariant 7: a failed
    sync leaves the last good snapshot intact and queryable — it must not take
    the process down with it).
    """

    def __init__(self, jobs: Sequence[Job], *, clock: Callable[[], datetime] | None = None):
        if not jobs:
            raise ValueError("scheduler needs at least one job")
        self._jobs = list(jobs)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM/SIGINT so container shutdown is not a kill."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.request_stop())

    def run(self, *, max_iterations: int | None = None) -> int:
        """Loop until stopped. ``max_iterations`` bounds it for tests."""
        now = self._clock()
        schedule = {job.name: job.spec.next_after(now) for job in self._jobs}
        for job in self._jobs:
            log.info(
                "scheduled %s (%s), next run %s",
                job.name,
                job.spec.expression,
                schedule[job.name].isoformat(),
            )

        iterations = 0
        while not self._stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break

            due_name = min(schedule, key=lambda name: schedule[name])
            due_at = schedule[due_name]
            delay = (due_at - self._clock()).total_seconds()
            if delay > 0 and self._stop.wait(delay):
                break

            job = next(j for j in self._jobs if j.name == due_name)
            log.info("running %s", job.name)
            try:
                job.run()
            except Exception:
                log.exception("%s failed; the last good snapshot is untouched", job.name)

            schedule[due_name] = job.spec.next_after(self._clock())
            log.info("next %s at %s", job.name, schedule[due_name].isoformat())
            iterations += 1

        log.info("scheduler stopped")
        return iterations
