"""The dependency-free cron parser and the ``serve`` loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tns_mirror_server.scheduler import CronSpec, Job, Scheduler


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression,after,expected",
    [
        # The three schedules the server actually ships with.
        ("0 0 * * *", at(2026, 7, 26, 12, 0), at(2026, 7, 27, 0, 0)),
        ("10 * * * *", at(2026, 7, 26, 12, 0), at(2026, 7, 26, 12, 10)),
        ("10 * * * *", at(2026, 7, 26, 12, 30), at(2026, 7, 26, 13, 10)),
        ("30 0 * * *", at(2026, 7, 26, 0, 29), at(2026, 7, 26, 0, 30)),
        # Steps, ranges and lists.
        ("*/15 * * * *", at(2026, 7, 26, 12, 3), at(2026, 7, 26, 12, 15)),
        ("0 9-17 * * *", at(2026, 7, 26, 20, 0), at(2026, 7, 27, 9, 0)),
        ("0 0 1,15 * *", at(2026, 7, 2, 0, 0), at(2026, 7, 15, 0, 0)),
        # Month rollover and leap day.
        ("0 0 * * *", at(2026, 12, 31, 23, 59), at(2027, 1, 1, 0, 0)),
        ("0 0 29 2 *", at(2024, 3, 1, 0, 0), at(2028, 2, 29, 0, 0)),
    ],
)
def test_next_fire_time(expression, after, expected):
    assert CronSpec.parse(expression).next_after(after) == expected


def test_next_is_strictly_after_the_given_moment():
    # Otherwise a job that finishes inside its own minute reschedules onto itself
    # and runs in a tight loop.
    spec = CronSpec.parse("10 * * * *")
    assert spec.next_after(at(2026, 7, 26, 12, 10)) == at(2026, 7, 26, 13, 10)


def test_day_of_month_and_day_of_week_are_or_ed():
    # Standard cron semantics: with both fields restricted the job runs on either
    # match. AND-ing them silently skips most runs.
    spec = CronSpec.parse("0 0 13 * 5")  # the 13th, or any Friday
    assert spec.next_after(at(2026, 7, 1)) == at(2026, 7, 3)  # first Friday
    assert spec.next_after(at(2026, 7, 4)) == at(2026, 7, 10)  # next Friday

    friday_the_13th = CronSpec.parse("0 0 13 * *").next_after(at(2026, 11, 1))
    assert friday_the_13th == at(2026, 11, 13)


def test_sunday_is_accepted_as_both_0_and_7():
    assert CronSpec.parse("0 0 * * 0").next_after(at(2026, 7, 26, 1)) == at(2026, 8, 2)
    assert CronSpec.parse("0 0 * * 7").next_after(at(2026, 7, 26, 1)) == at(2026, 8, 2)


@pytest.mark.parametrize(
    "expression",
    [
        "0 0 * *",  # too few fields
        "0 0 * * * *",  # too many
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # day-of-month is 1-based
        "*/0 * * * *",  # zero step
        "abc * * * *",
        "5-1 * * * *",  # inverted range
        "",
    ],
)
def test_invalid_expressions_are_rejected(expression):
    with pytest.raises(ValueError):
        CronSpec.parse(expression)


# --- the loop ---------------------------------------------------------------


class AdvancingClock:
    """Jumps an hour per call, so scheduled work is always already due."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(hours=1)
        return self.now


def test_scheduler_runs_due_jobs():
    runs: list[str] = []
    jobs = [Job("full", CronSpec.parse("* * * * *"), lambda: runs.append("full"))]

    iterations = Scheduler(jobs, clock=AdvancingClock(at(2026, 7, 26))).run(max_iterations=3)

    assert iterations == 3
    assert runs == ["full"] * 3


def test_a_failing_job_does_not_take_the_process_down():
    # Invariant 7: a failed sync leaves the last good snapshot intact and
    # queryable. It must not stop the delta job from running an hour later.
    runs: list[str] = []

    def explode() -> None:
        runs.append("boom")
        raise RuntimeError("TNS returned garbage")

    jobs = [
        Job("full", CronSpec.parse("* * * * *"), explode),
        Job("delta", CronSpec.parse("* * * * *"), lambda: runs.append("delta")),
    ]

    Scheduler(jobs, clock=AdvancingClock(at(2026, 7, 26))).run(max_iterations=4)

    assert "boom" in runs
    assert "delta" in runs


def test_request_stop_ends_the_loop():
    scheduler = Scheduler(
        [Job("j", CronSpec.parse("* * * * *"), lambda: None)],
        clock=AdvancingClock(at(2026, 7, 26)),
    )
    scheduler.request_stop()

    assert scheduler.run(max_iterations=10) == 0


def test_scheduler_needs_at_least_one_job():
    with pytest.raises(ValueError, match="at least one job"):
        Scheduler([])
