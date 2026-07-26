"""Client integration tests against a real Postgres seeded from the contract.

The database is built by applying ``schema/schema_v1.sql`` — the *published*
contract, not anything the client owns — so these tests exercise the same thing a
consumer would connect to. Skipped unless ``TNS_TEST_DSN`` is set:

    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pg --name tns-pg postgres:16
    TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tns_mirror_client import (
    MirrorUnavailable,
    SchemaVersionError,
    TnsMirror,
)
from tns_mirror_client.geometry import angular_separation_deg

from .test_contract import SCHEMA_FILE

pytestmark = pytest.mark.integration

DSN = os.environ.get("TNS_TEST_DSN")

if not DSN:  # pragma: no cover - environment dependent
    pytest.skip("set TNS_TEST_DSN to run client integration tests", allow_module_level=True)

NOW = datetime.now(UTC)

# A small sky: a target, a close neighbour, a far one, and objects parked on the
# awkward parts of the sphere.
OBJECTS = [
    # objid, name,        ra,      dec,     type,     redshift
    (1, "AT2026abc", 203.10000, 10.20000, None, None),
    (2, "SN2026xyz", 203.10050, 10.20000, "SN Ia", 0.031),  # ~1.8" away
    (3, "AT2026far", 203.20000, 10.20000, None, None),  # ~354" away
    (4, "AT2026seam", 359.99000, 0.00000, None, None),  # just below the seam
    (5, "AT2026wrap", 0.01000, 0.00000, None, None),  # just above it
    (6, "AT2026pole", 12.50000, 89.90000, None, None),
    (7, "AT2026pole2", 190.00000, 89.90000, None, None),  # far in RA, near in angle
]


@pytest.fixture
def schema():
    """A throwaway Postgres schema with the published contract applied."""
    name = f"tns_client_{uuid.uuid4().hex[:12]}"
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")

    # The published file is rendered at defaults (public.tns_objects); point it
    # at an isolated schema so tests can run side by side.
    ddl = ddl.replace("public.", f"{name}.").replace(
        "CREATE SCHEMA IF NOT EXISTS public", f"CREATE SCHEMA IF NOT EXISTS {name}"
    )

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(ddl)
        with conn.cursor() as cur:
            for objid, obj_name, ra, dec, obj_type, redshift in OBJECTS:
                cur.execute(
                    f'INSERT INTO {name}.tns_objects (objid, name, ra, "dec", "type", '
                    f"redshift, internal_names, source_snapshot, refreshed_at) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, current_date, now())",
                    (objid, obj_name, ra, dec, obj_type, redshift, "ZTF26aaa, ATLAS26x"),
                )
            cur.execute(
                f"UPDATE {name}.tns_mirror_meta SET last_full_sync_at = now(), "
                f"last_delta_sync_at = now(), last_source_snapshot = current_date "
                f"WHERE id = 1"
            )

    yield name

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@pytest.fixture
def tns(schema):
    with TnsMirror(dsn=DSN, schema=schema) as client:
        yield client


# --- lookups ----------------------------------------------------------------


def test_by_name_returns_a_fully_typed_object(tns):
    hit = tns.by_name("SN2026xyz")

    assert hit is not None
    assert hit.objid == 2
    assert hit.name == "SN2026xyz"
    assert hit.ra == pytest.approx(203.1005)
    assert hit.dec == pytest.approx(10.2)
    assert hit.type == "SN Ia"
    assert hit.redshift == pytest.approx(0.031)
    assert hit.is_classified is True
    assert hit.internal_name_list == ["ZTF26aaa", "ATLAS26x"]
    assert hit.separation_arcsec is None  # not a cone query


def test_by_name_returns_none_for_an_unknown_object(tns):
    assert tns.by_name("SN1987A") is None


def test_unclassified_objects_report_so(tns):
    hit = tns.by_name("AT2026abc")
    assert hit.type is None
    assert hit.redshift is None
    assert hit.is_classified is False


def test_by_objid(tns):
    assert tns.by_objid(3).name == "AT2026far"
    assert tns.by_objid(999999) is None


def test_by_names_fetches_many_in_one_round_trip(tns):
    found = tns.by_names(["AT2026abc", "SN2026xyz", "SN1987A"])

    assert set(found) == {"AT2026abc", "SN2026xyz"}
    assert found["SN2026xyz"].objid == 2


def test_by_names_with_nothing_asked_for(tns):
    assert tns.by_names([]) == {}


def test_count(tns):
    assert tns.count() == len(OBJECTS)


# --- cone search ------------------------------------------------------------


def test_nearest_picks_the_closest_within_the_radius(tns):
    hit = tns.nearest(ra=203.10040, dec=10.2, radius_arcsec=3.0)

    assert hit is not None
    assert hit.name == "SN2026xyz"
    assert hit.separation_arcsec == pytest.approx(0.35, abs=0.05)


def test_nearest_returns_none_outside_the_radius(tns):
    # AT2026far is ~354" away; a 3" cone must not reach it.
    assert tns.nearest(ra=203.2, dec=10.25, radius_arcsec=3.0) is None


def test_the_radius_boundary_is_tight(tns):
    """A cone centred on object 1 includes object 2 only once the radius reaches it."""
    separation = angular_separation_deg(203.1, 10.2, 203.1005, 10.2) * 3600
    assert separation == pytest.approx(1.77, abs=0.05)

    just_inside = tns.search(ra=203.1, dec=10.2, radius_arcsec=separation * 1.01)
    just_outside = tns.search(ra=203.1, dec=10.2, radius_arcsec=separation * 0.99)

    assert [m.objid for m in just_inside] == [1, 2]
    assert [m.objid for m in just_outside] == [1]  # the neighbour drops out


def test_nearest_is_the_closest_not_merely_the_first_in_range(tns):
    # Queried from just beside object 2, object 2 must win even though object 1
    # is also inside the radius.
    hit = tns.nearest(ra=203.10052, dec=10.2, radius_arcsec=10.0)
    assert hit.objid == 2


def test_search_returns_everything_in_range_nearest_first(tns):
    matches = tns.search(ra=203.1, dec=10.2, radius_arcsec=400.0)

    assert [m.name for m in matches] == ["AT2026abc", "SN2026xyz", "AT2026far"]
    separations = [m.separation_arcsec for m in matches]
    assert separations == sorted(separations)
    assert matches[0].separation_arcsec == pytest.approx(0.0, abs=1e-6)


def test_search_honours_a_limit(tns):
    matches = tns.search(ra=203.1, dec=10.2, radius_arcsec=400.0, limit=2)
    assert len(matches) == 2


def test_search_finds_nothing_in_empty_sky(tns):
    assert tns.search(ra=100.0, dec=-30.0, radius_arcsec=5.0) == []


def test_separation_agrees_with_the_python_implementation(tns):
    # The SQL and the pure-Python geometry must not drift apart.
    match = tns.search(ra=203.1, dec=10.2, radius_arcsec=400.0)[2]
    expected = angular_separation_deg(203.1, 10.2, match.ra, match.dec) * 3600

    assert match.separation_arcsec == pytest.approx(expected, rel=1e-9)


# --- the awkward parts of the sphere ----------------------------------------


def test_a_cone_across_the_0_360_seam_finds_both_sides(tns):
    # The whole reason for two OR-ed RA ranges. A naive BETWEEN finds nothing.
    matches = tns.search(ra=0.0, dec=0.0, radius_arcsec=100.0)

    assert {m.name for m in matches} == {"AT2026seam", "AT2026wrap"}


def test_a_cone_just_below_the_seam_also_wraps(tns):
    matches = tns.search(ra=359.995, dec=0.0, radius_arcsec=100.0)
    assert {m.name for m in matches} == {"AT2026seam", "AT2026wrap"}


def test_near_the_pole_ra_distance_does_not_hide_a_close_object(tns):
    # These two are 177.5 degrees apart in RA but only ~0.2 degrees apart on the
    # sky. Constraining RA near the pole would lose the match entirely.
    matches = tns.search(ra=12.5, dec=89.9, radius_arcsec=3600.0)

    assert {m.name for m in matches} == {"AT2026pole", "AT2026pole2"}


def test_an_exact_positional_match_does_not_raise(tns):
    # acos(1.0000000001) is a domain error; the clamp is what prevents it.
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=1.0)

    assert hit.objid == 1
    assert hit.separation_arcsec == pytest.approx(0.0, abs=1e-6)


def test_a_zero_radius_still_matches_an_exact_position(tns):
    assert tns.nearest(ra=203.1, dec=10.2, radius_arcsec=0.0).objid == 1


@pytest.mark.parametrize("radius", [-1.0, -0.001])
def test_a_negative_radius_is_rejected(tns, radius):
    with pytest.raises(ValueError, match="must not be negative"):
        tns.nearest(ra=1.0, dec=1.0, radius_arcsec=radius)


def test_a_nonsense_limit_is_rejected(tns):
    with pytest.raises(ValueError, match="at least 1"):
        tns.search(ra=1.0, dec=1.0, radius_arcsec=1.0, limit=0)


# --- metadata and versioning ------------------------------------------------


def test_meta_reports_version_and_freshness(tns):
    meta = tns.meta()

    assert meta.schema_version == 1
    assert meta.last_full_sync_at is not None
    assert meta.age < timedelta(minutes=5)
    assert meta.is_fresh() is True


def test_a_stale_mirror_is_reported_as_stale(schema):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {schema}.tns_mirror_meta SET last_full_sync_at = %s, "
            f"last_delta_sync_at = %s WHERE id = 1",
            (NOW - timedelta(days=21), NOW - timedelta(days=21)),
        )

    with TnsMirror(dsn=DSN, schema=schema) as tns:
        meta = tns.meta()
        assert meta.is_fresh() is False
        assert meta.age > timedelta(days=20)


def test_a_mirror_that_never_synced_is_never_fresh(schema):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {schema}.tns_mirror_meta SET last_full_sync_at = NULL, "
            f"last_delta_sync_at = NULL WHERE id = 1"
        )

    with TnsMirror(dsn=DSN, schema=schema) as tns:
        meta = tns.meta()
        assert meta.last_sync_at is None
        assert meta.age is None
        assert meta.is_fresh() is False


def test_a_future_schema_version_is_refused_at_connect(schema):
    # The contract may have renamed a column out from under this client, so
    # guessing is worse than failing.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"UPDATE {schema}.tns_mirror_meta SET schema_version = 2 WHERE id = 1")

    client = TnsMirror(dsn=DSN, schema=schema)
    with pytest.raises(SchemaVersionError, match="schema v2") as excinfo:
        client.count()

    assert ">=2,<3" in str(excinfo.value)
    # A client that refused to connect must not leave a socket behind.
    assert client._conn is None


def test_the_version_check_can_be_skipped_deliberately(schema):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"UPDATE {schema}.tns_mirror_meta SET schema_version = 2 WHERE id = 1")

    with TnsMirror(dsn=DSN, schema=schema, check_schema_version=False) as tns:
        assert tns.count() == len(OBJECTS)


def test_a_database_that_is_not_a_mirror_says_so():
    with (
        TnsMirror(dsn=DSN, schema="pg_catalog") as other,
        pytest.raises(MirrorUnavailable, match="does not look like a tns-mirror"),
    ):
        other.meta()


def test_an_unreachable_mirror_says_so():
    bad = "postgresql://nobody:nobody@127.0.0.1:1/none?connect_timeout=1"
    with pytest.raises(MirrorUnavailable, match="could not connect"):
        TnsMirror(dsn=bad).count()


# --- read-only posture -------------------------------------------------------


def test_the_session_refuses_writes_even_with_a_privileged_role(tns, schema):
    """Defence in depth: the client has no write path, and now nor does its session.

    These tests connect as the owner, so the *role* could write. The client sets
    the session read-only, which means a bug in this library still cannot.
    """
    conn = tns._connect()

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(f"DELETE FROM {schema}.tns_objects")


def test_a_borrowed_connection_is_not_closed_by_the_client(schema):
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        with TnsMirror(connection=conn, schema=schema) as tns:
            assert tns.count() == len(OBJECTS)
        assert not conn.closed, "the client closed a connection it did not open"
    finally:
        conn.close()


def test_a_client_needs_something_to_connect_with():
    with pytest.raises(ValueError, match="needs either a dsn or a connection"):
        TnsMirror()
