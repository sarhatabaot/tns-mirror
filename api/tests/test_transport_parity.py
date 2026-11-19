"""The client's two transports give identical answers.

One Postgres, seeded from the published contract. The same questions go to
``TnsMirror(dsn=...)`` directly and to ``TnsMirror(url=...)`` through this API's
real application, and every answer must be equal: the same dataclasses, the same
separations to the last bit, the same order.

Needs a client with the HTTP transport (1.1.0 or later). The API's lockfile pins
the released client, so CI installs the one in this commit before running this;
against an older client it skips. Skipped too without ``TNS_TEST_DSN``.
"""

from __future__ import annotations

import inspect
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from litestar.testing import TestClient
from tns_mirror_client import TnsMirror

from tns_mirror_api.app import create_app
from tns_mirror_api.config import Config

pytestmark = pytest.mark.integration

DSN = os.environ.get("TNS_TEST_DSN")
if not DSN:  # pragma: no cover - environment dependent
    pytest.skip("set TNS_TEST_DSN to run the parity tests", allow_module_level=True)
if "url" not in inspect.signature(TnsMirror).parameters:  # pragma: no cover
    pytest.skip("the installed client has no HTTP transport", allow_module_level=True)

# After the skip: an older client has no such error to import.
from tns_mirror_client import QueryLimitExceeded  # noqa: E402

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema" / "schema_v1.sql"
BASE = "http://testserver.local/"
KEY = "parity-test-key"

# The awkward parts of the sphere, and every shape internal_names takes.
OBJECTS = [
    # objid, name,        ra,        dec,      internal_names
    (1, "AT2026abc", 203.10000, 10.20000, "ZTF26aaa, ATLAS26x"),
    (2, "SN2026xyz", 203.10050, 10.20000, "ZTF26bbb,ATLAS26y"),  # ~1.8" away
    (3, "AT2026far", 203.20000, 10.20000, None),  # ~354" away
    (4, "AT2026seam", 359.99000, 0.00000, ""),  # just below the seam
    (5, "AT2026wrap", 0.01000, 0.00000, "PS26a"),  # just above it
    (6, "AT2026pole", 12.50000, 89.90000, None),
    (7, "AT2026pole2", 190.00000, 89.90000, None),  # far in RA, near in angle
    (8, "AT2026south", 77.00000, -89.95000, None),
    (9, "AT2026twin", 203.10050, 10.20000, None),  # same place as objid 2: a tie
]


@pytest.fixture
def schema():
    name = f"tns_parity_{uuid.uuid4().hex[:12]}"
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    ddl = ddl.replace("public.", f"{name}.").replace(
        "CREATE SCHEMA IF NOT EXISTS public", f"CREATE SCHEMA IF NOT EXISTS {name}"
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(ddl)
        with conn.cursor() as cur:
            for objid, obj_name, ra, dec, internal in OBJECTS:
                classified = objid == 2
                cur.execute(
                    f'INSERT INTO {name}.tns_objects (objid, name, ra, "dec", "type", '
                    f"redshift, discoverydate, discoverymag, internal_names, "
                    f"reporting_group, source_snapshot, refreshed_at) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,current_date,now())",
                    (
                        objid,
                        obj_name,
                        ra,
                        dec,
                        "SN Ia" if classified else None,
                        0.031 if classified else None,
                        "2026-01-02 03:04:05.678+00" if classified else None,
                        18.25 if classified else None,
                        internal,
                        "ZTF" if classified else None,
                    ),
                )
            cur.execute(
                f"UPDATE {name}.tns_mirror_meta SET last_full_sync_at = now(), "
                f"last_delta_sync_at = now(), last_source_snapshot = current_date WHERE id = 1"
            )
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


class _Session:
    """Enough of ``requests.Session`` to drive the app in-process.

    The client hands its requests to whatever session it is given. This one
    passes them to Litestar's test client, which runs the real application,
    middleware and lifespan, and whose responses have the same ``status_code``,
    ``json()``, ``text`` and ``headers`` the client reads.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.seen_keys: list[str | None] = []

    def get(self, url: str, *, params=None, headers=None, timeout=None, allow_redirects=True):
        assert timeout is not None, "the client must always set a timeout"
        self.seen_keys.append((headers or {}).get("X-API-Key"))
        return self.client.get(
            "/" + url.removeprefix(BASE),
            params=params,
            headers=headers,
            follow_redirects=allow_redirects,
        )

    def close(self) -> None:  # pragma: no cover - a lent session is never closed
        raise AssertionError("the client closed a session it did not open")


@pytest.fixture
def pair(schema):
    """The same mirror, over SQL and over HTTP."""

    def make(**config: Any):
        app = create_app(Config(dsn=DSN, schema=schema, rate_limit_per_minute=10_000, **config))
        return TestClient(app=app)

    opened: list[Any] = []

    def build(**config: Any) -> tuple[TnsMirror, TnsMirror, _Session]:
        client = make(**config).__enter__()
        session = _Session(client)
        sql = TnsMirror(dsn=DSN, schema=schema)
        http = TnsMirror(url=BASE, api_key=KEY, session=session)
        opened.append((client, sql, http))
        return sql, http, session

    yield build

    for client, sql, http in opened:
        sql.close()
        http.close()
        client.__exit__(None, None, None)


CONES = [
    pytest.param(203.1004, 10.2, 3.0, id="target"),
    pytest.param(203.1, 10.2, 400.0, id="three-and-a-tie"),
    pytest.param(203.1005, 10.2, 0.0, id="zero-radius-tie"),
    pytest.param(0.0, 0.0, 100.0, id="seam-centre"),
    pytest.param(359.995, 0.0, 60.0, id="seam-below"),
    pytest.param(0.005, 0.0, 60.0, id="seam-above"),
    pytest.param(100.0, 89.9, 1200.0, id="north-pole-near"),
    pytest.param(0.0, 90.0, 400.0, id="north-pole-exact"),
    pytest.param(250.0, -90.0, 300.0, id="south-pole-exact"),
    pytest.param(100.0, -40.0, 3.0, id="empty"),
]


@pytest.mark.parametrize(("ra", "dec", "radius"), CONES)
def test_search_is_identical(pair, ra, dec, radius):
    sql, http, _ = pair()
    expected = sql.search(ra, dec, radius)
    assert http.search(ra, dec, radius) == expected
    for limit in (1, 2, 5):
        assert http.search(ra, dec, radius, limit=limit) == expected[:limit]


@pytest.mark.parametrize(("ra", "dec", "radius"), CONES)
def test_nearest_is_identical(pair, ra, dec, radius):
    sql, http, _ = pair()
    assert http.nearest(ra, dec, radius) == sql.nearest(ra, dec, radius)


def test_the_cases_above_are_not_vacuous(pair):
    sql, _, _ = pair()
    assert [o.objid for o in sql.search(203.1, 10.2, 400.0)] == [1, 2, 9, 3]
    assert {o.objid for o in sql.search(0.0, 0.0, 100.0)} == {4, 5}
    assert {o.objid for o in sql.search(0.0, 90.0, 400.0)} == {6, 7}
    assert [o.objid for o in sql.search(250.0, -90.0, 300.0)] == [8]


def test_separations_agree_to_the_last_bit(pair):
    sql, http, _ = pair()
    a = sql.search(203.1004, 10.2, 400.0)
    b = http.search(203.1004, 10.2, 400.0)
    assert [o.separation_arcsec for o in a] == [o.separation_arcsec for o in b]
    assert all(o.separation_arcsec is not None for o in b)


def test_lookups_are_identical(pair):
    sql, http, _ = pair()
    for name in ("SN2026xyz", "AT2026seam", "AT2026far", "SN1987A"):
        assert http.by_name(name) == sql.by_name(name)
    for objid in (1, 2, 4, 999):
        assert http.by_objid(objid) == sql.by_objid(objid)
    names = [o[1] for o in OBJECTS] + ["SN1987A"]
    assert http.by_names(names) == sql.by_names(names)


def test_internal_names_come_back_as_stored(pair):
    _, http, _ = pair()
    stored = {o[0]: o[4] for o in OBJECTS}
    for objid, internal in stored.items():
        assert http.by_objid(objid).internal_names == internal


def test_count_and_meta_are_identical(pair):
    sql, http, _ = pair()
    assert http.count() == sql.count() == len(OBJECTS)
    assert http.meta() == sql.meta()


def test_the_key_is_sent_on_every_request(pair):
    _, http, session = pair()
    http.nearest(203.1, 10.2)
    http.count()
    assert session.seen_keys and set(session.seen_keys) == {KEY}


def test_a_server_cap_is_an_error_not_a_short_answer(pair):
    _, http, _ = pair(max_limit=2, max_radius_arcsec=500.0)
    with pytest.raises(QueryLimitExceeded):
        http.search(203.1, 10.2, 400.0)  # four objects, two served
    with pytest.raises(QueryLimitExceeded):
        http.search(203.1, 10.2, 600.0)  # radius clamped to 500
    assert len(http.search(203.1, 10.2, 400.0, limit=2)) == 2
