"""Engine behaviour, against the in-memory store. No network, no database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from tns_mirror_server.adapters.memory import FakeSource, MemoryStore
from tns_mirror_server.engine import catch_up, delta_sync, full_sync, snapshot_for_hour
from tns_mirror_server.ports.source import TnsRecord

NOW = datetime(2026, 7, 26, 11, 30, tzinfo=UTC)


def record(objid: int | None, name: str, ra: float = 1.0, dec: float = 2.0) -> TnsRecord:
    return TnsRecord(objid=objid, name=name, ra=ra, dec=dec)


# --- full snapshot ----------------------------------------------------------


def test_full_snapshot_replaces_everything_and_drops_depublished_objects():
    store = MemoryStore()
    store.rows = {999: record(999, "ATgone")}

    source = FakeSource(full=[record(1, "AT1"), record(2, "SN2")])
    result = full_sync(store, source)

    assert result.rows_written == 2
    assert store.names() == {"AT1", "SN2"}
    assert 999 not in store.rows  # a de-published object actually disappears
    assert store.replace_calls == 1


def test_full_snapshot_drops_rows_with_no_stable_id_and_counts_them():
    # objid is the primary key and the upsert target; a row without one cannot be
    # stored. One malformed line must not abort a whole refresh.
    store = MemoryStore()
    source = FakeSource(full=[record(1, "AT1"), record(None, "ATnoid"), record(2, "SN2")])

    result = full_sync(store, source)

    assert result.rows_written == 2
    assert result.rows_skipped == 1
    assert store.names() == {"AT1", "SN2"}


def test_full_snapshot_downloads_before_writing():
    store = MemoryStore()
    source = FakeSource(full=[record(1, "AT1")])

    full_sync(store, source)

    assert source.downloaded == [None]  # None == the full snapshot


def test_full_snapshot_can_reuse_an_existing_file():
    store = MemoryStore()
    source = FakeSource(full=[record(1, "AT1")])

    full_sync(store, source, reuse_existing=True)

    assert store.count() == 1


# --- hourly delta -----------------------------------------------------------


def test_delta_upserts_on_objid():
    store = MemoryStore()
    full_sync(store, FakeSource(full=[record(1, "AT1"), record(2, "SN2")]))

    source = FakeSource(deltas={5: [record(1, "AT1renamed"), record(3, "AT3")]})
    result = delta_sync(store, source, 5, now=NOW)

    assert result.rows_written == 2
    assert store.rows[1].name == "AT1renamed"  # updated in place
    assert store.rows[3].name == "AT3"  # inserted
    assert store.rows[2].name == "SN2"  # untouched


def test_delta_is_idempotent():
    store = MemoryStore()
    source = FakeSource(deltas={5: [record(1, "AT1")]})

    delta_sync(store, source, 5, now=NOW)
    snapshot = dict(store.rows)
    delta_sync(store, source, 5, now=NOW)

    assert store.rows == snapshot


def test_a_quiet_hour_is_a_no_op_not_a_failure():
    # TNS publishes an unparseable/empty file for an hour with no edits. Only the
    # full refresh is strict (invariant 7).
    store = MemoryStore()
    full_sync(store, FakeSource(full=[record(1, "AT1")]))

    result = delta_sync(store, FakeSource(deltas={}), 5, now=NOW)

    assert result.rows_written == 0
    assert store.count() == 1  # last good data still there


def test_delta_skips_rows_without_a_stable_id():
    store = MemoryStore()
    source = FakeSource(deltas={5: [record(None, "ATnoid"), record(7, "AT7")]})

    result = delta_sync(store, source, 5, now=NOW)

    assert result.rows_written == 1
    assert result.rows_skipped == 1
    assert store.names() == {"AT7"}


# --- catch-up ---------------------------------------------------------------


def test_catch_up_applies_oldest_to_newest():
    # An object edited in several hours of the window must end at its latest
    # state, which only holds if the deltas are applied in file order.
    store = MemoryStore()
    source = FakeSource(
        deltas={
            9: [record(1, "state-at-09")],
            10: [record(1, "state-at-10")],
            11: [record(1, "state-at-11")],
        }
    )

    result = catch_up(store, source, hours=3, end_hour=11, now=NOW)

    assert result.hours == [9, 10, 11]
    assert source.downloaded == [9, 10, 11]
    assert store.rows[1].name == "state-at-11"


def test_catch_up_wraps_across_midnight():
    store = MemoryStore()
    source = FakeSource(deltas={23: [], 0: [], 1: []})

    result = catch_up(store, source, hours=3, end_hour=1, now=NOW)

    assert result.hours == [23, 0, 1]


def test_catch_up_defaults_to_the_full_day():
    store = MemoryStore()
    source = FakeSource(deltas={})

    result = catch_up(store, source, now=NOW, end_hour=11)

    assert len(result.hours) == 24
    assert result.hours[0] == 12  # 24 hours back from 11 is 12
    assert result.hours[-1] == 11


@pytest.mark.parametrize("requested,expected", [(0, 1), (-5, 1), (99, 24)])
def test_catch_up_window_is_clamped_to_one_day(requested, expected):
    store = MemoryStore()
    result = catch_up(store, FakeSource(deltas={}), hours=requested, end_hour=11, now=NOW)
    assert len(result.hours) == expected


def test_catch_up_stops_early_on_rate_limit_and_keeps_what_it_applied():
    store = MemoryStore()
    response = requests.Response()
    response.status_code = 429
    source = FakeSource(
        deltas={
            9: [record(1, "applied")],
            10: requests.HTTPError("rate limited", response=response),
            11: [record(2, "never-reached")],
        }
    )

    result = catch_up(store, source, hours=3, end_hour=11, now=NOW)

    assert result.stopped_early is True
    assert source.downloaded == [9, 10]  # hour 11 never attempted
    assert store.names() == {"applied"}  # hour 9 is committed and stays


def test_other_http_errors_are_not_swallowed():
    store = MemoryStore()
    response = requests.Response()
    response.status_code = 500
    source = FakeSource(deltas={11: requests.HTTPError("boom", response=response)})

    with pytest.raises(requests.HTTPError):
        catch_up(store, source, hours=1, end_hour=11, now=NOW)


def test_catch_up_throttles_between_downloads_but_not_before_the_first(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("tns_mirror_server.engine.time.sleep", slept.append)
    source = FakeSource(deltas={9: [], 10: [], 11: []}, throttle=10.0)

    catch_up(MemoryStore(), source, hours=3, end_hour=11, now=NOW)

    assert slept == [10.0, 10.0]  # three downloads, two gaps


# --- snapshot dating --------------------------------------------------------


def test_wrapped_catch_up_hours_are_dated_to_the_previous_day():
    # A window run at 01:00 covering [23, 00, 01] must date hour 23 to yesterday,
    # or every wrapped window stamps source_snapshot a day ahead.
    now = datetime(2026, 7, 26, 1, 5, tzinfo=UTC)

    assert snapshot_for_hour(1, now).isoformat() == "2026-07-26"
    assert snapshot_for_hour(0, now).isoformat() == "2026-07-26"
    assert snapshot_for_hour(23, now).isoformat() == "2026-07-25"


def test_delta_result_records_the_snapshot_date():
    store = MemoryStore()
    now = datetime(2026, 7, 26, 1, 5, tzinfo=UTC)
    source = FakeSource(deltas={23: [record(1, "AT1")]})

    result = delta_sync(store, source, 23, now=now)

    assert result.snapshot.isoformat() == "2026-07-25"
    assert store.snapshots[1].isoformat() == "2026-07-25"


def test_results_describe_themselves_for_the_operator():
    store = MemoryStore()
    result = full_sync(store, FakeSource(full=[record(1, "AT1"), record(None, "x")]))
    assert "full snapshot" in result.describe()
    assert "1 skipped" in result.describe()

    catchup = catch_up(store, FakeSource(deltas={11: [record(1, "AT1")]}), 1, end_hour=11)
    assert "catch-up over 1h" in catchup.describe()
