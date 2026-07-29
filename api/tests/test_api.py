"""API tests against a real Postgres seeded from the published contract.

Seeded from ``schema/schema_v1.sql`` — the file consumers actually read — so
these exercise the same thing a deployment would serve. Skipped unless
``TNS_TEST_DSN`` is set:

    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pg --name tns-pg postgres:16
    TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from litestar.testing import TestClient

from tns_mirror_api.app import create_app
from tns_mirror_api.config import Config, _normalise_prefix

pytestmark = pytest.mark.integration

DSN = os.environ.get("TNS_TEST_DSN")
if not DSN:  # pragma: no cover - environment dependent
    pytest.skip("set TNS_TEST_DSN to run API tests", allow_module_level=True)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema" / "schema_v1.sql"

# A target, a close neighbour ~1.8" away, a far one, and the 0/360 seam.
OBJECTS = [
    (1, "AT2026abc", 203.10000, 10.20000, None, None),
    (2, "SN2026xyz", 203.10050, 10.20000, "SN Ia", 0.031),
    (3, "AT2026far", 203.20000, 10.20000, None, None),
    (4, "AT2026seam", 359.99000, 0.00000, None, None),
    (5, "AT2026wrap", 0.01000, 0.00000, None, None),
]


@pytest.fixture
def schema():
    name = f"tns_api_{uuid.uuid4().hex[:12]}"
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
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
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,current_date,now())",
                    (objid, obj_name, ra, dec, obj_type, redshift, "ZTF26aaa, ATLAS26x"),
                )
            cur.execute(
                f"UPDATE {name}.tns_mirror_meta SET last_full_sync_at = now(), "
                f"last_delta_sync_at = now(), last_source_snapshot = current_date WHERE id = 1"
            )
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def make_client(schema: str, **overrides) -> TestClient:
    """Drive the real application factory, middleware and lifespan included.

    Assembling the handlers by hand would test the handlers but not the app —
    and the rate limiter, the pool and the startup version check all live in
    the app.
    """
    if "root_path" in overrides:
        overrides["root_path"] = _normalise_prefix(overrides["root_path"])
    return TestClient(app=create_app(Config(dsn=DSN, schema=schema, **overrides)))


@pytest.fixture
def client(schema):
    with make_client(schema) as c:
        yield c


# --- health ------------------------------------------------------------------


def test_healthz_does_not_touch_the_database(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_ready_on_a_fresh_mirror(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["schema_version"] == 1


def test_readyz_refuses_a_stale_mirror(schema):
    """A mirror that stopped syncing still answers every query confidently."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {schema}.tns_mirror_meta "
            f"SET last_full_sync_at = now() - interval '21 days', "
            f"    last_delta_sync_at = now() - interval '21 days' "
            f"WHERE id = 1"
        )
    with make_client(schema) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert "stale" in response.json()["detail"]


# --- lookups -----------------------------------------------------------------


def test_by_name(client):
    body = client.get("/v1/objects/SN2026xyz").json()
    assert body["objid"] == 2
    assert body["type"] == "SN Ia"
    assert body["redshift"] == pytest.approx(0.031)
    # Split for the caller rather than handing over TNS's comma-separated string.
    assert body["internal_names"] == ["ZTF26aaa", "ATLAS26x"]
    assert body["separation_arcsec"] is None


def test_by_name_missing_is_404(client):
    assert client.get("/v1/objects/SN1987A").status_code == 404


def test_by_objid(client):
    assert client.get("/v1/objid/3").json()["name"] == "AT2026far"
    assert client.get("/v1/objid/999999").status_code == 404


def test_meta(client):
    body = client.get("/v1/meta").json()
    assert body["schema_version"] == 1
    assert body["rows"] == len(OBJECTS)
    assert body["stale"] is False


# --- cone search -------------------------------------------------------------


def test_nearest(client):
    body = client.get("/v1/nearest?ra=203.1004&dec=10.2&radius_arcsec=3").json()
    assert body["name"] == "SN2026xyz"
    assert body["separation_arcsec"] == pytest.approx(0.35, abs=0.05)


def test_nearest_with_nothing_in_range_is_404(client):
    assert client.get("/v1/nearest?ra=100&dec=-40&radius_arcsec=3").status_code == 404


def test_cone_returns_everything_nearest_first(client):
    body = client.get("/v1/cone?ra=203.1&dec=10.2&radius_arcsec=400").json()
    assert [r["name"] for r in body["results"]] == ["AT2026abc", "SN2026xyz", "AT2026far"]
    assert body["count"] == 3
    separations = [r["separation_arcsec"] for r in body["results"]]
    assert separations == sorted(separations)


def test_the_seam_is_handled_because_the_client_handles_it(client):
    """The geometry is not reimplemented here; this proves the transport keeps it."""
    body = client.get("/v1/cone?ra=0&dec=0&radius_arcsec=100").json()
    assert {r["name"] for r in body["results"]} == {"AT2026seam", "AT2026wrap"}


# --- limits ------------------------------------------------------------------


@pytest.mark.parametrize(
    "query", ["ra=400&dec=0", "ra=-1&dec=0", "ra=10&dec=91", "ra=10&dec=-91"]
)
def test_positions_off_the_sphere_are_rejected(client, query):
    assert client.get(f"/v1/nearest?{query}").status_code == 400


@pytest.mark.parametrize("radius", ["0", "-5"])
def test_a_non_positive_radius_is_rejected(client, radius):
    assert client.get(f"/v1/nearest?ra=1&dec=1&radius_arcsec={radius}").status_code == 400


def test_an_oversized_radius_is_clamped_not_rejected(schema):
    # Rate limiting does not stop one enormous query, and an error the caller
    # has to handle is worse than the largest answer we are willing to give.
    with make_client(schema, max_radius_arcsec=60.0) as client:
        body = client.get("/v1/cone?ra=203.1&dec=10.2&radius_arcsec=999999").json()
    assert body["radius_arcsec"] == 60.0


def test_an_oversized_limit_is_clamped(schema):
    with make_client(schema, max_limit=2) as client:
        body = client.get("/v1/cone?ra=203.1&dec=10.2&radius_arcsec=400&limit=100").json()
    assert body["count"] == 2


# --- rate limiting -----------------------------------------------------------


def test_the_limit_returns_429_and_advertises_itself(schema):
    with make_client(schema, rate_limit_per_minute=3) as client:
        codes = [client.get("/v1/meta").status_code for _ in range(5)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_healthz_is_never_rate_limited(schema):
    # An orchestrator polls this; throttling it would make the service look
    # unhealthy exactly when it is busiest.
    with make_client(schema, rate_limit_per_minute=2) as client:
        codes = [client.get("/healthz").status_code for _ in range(6)]
    assert codes == [200] * 6


# --- running behind a reverse proxy -----------------------------------------


def test_routes_are_served_under_a_configured_prefix(schema):
    """Mounted at /tns, the app must answer /tns/... and not /..."""
    with make_client(schema, root_path="/tns") as client:
        assert client.get("/tns/healthz").status_code == 200
        assert client.get("/tns/v1/meta").json()["schema_version"] == 1
        assert client.get("/tns/v1/objects/AT2026abc").json()["objid"] == 1
        # The unprefixed paths must no longer exist, or a misconfigured proxy
        # would appear to work while serving the wrong URLs.
        assert client.get("/healthz").status_code == 404
        assert client.get("/v1/meta").status_code == 404


@pytest.mark.parametrize("raw", ["tns", "/tns", "/tns/", "tns/"])
def test_prefix_spelling_does_not_matter(schema, raw):
    # The difference is invisible in a browser and maddening in a config file.
    with make_client(schema, root_path=raw) as client:
        assert client.get("/tns/healthz").status_code == 200


def test_openapi_advertises_the_prefix(schema):
    # /docs offers a "try it" button; without the server URL it would point at
    # the origin root and 404 for anyone reaching the service through a proxy.
    with make_client(schema, root_path="/tns") as client:
        spec = client.get("/tns/docs/openapi.json").json()
    assert spec["servers"][0]["url"] == "/tns"


def test_liveness_is_still_unthrottled_when_mounted(schema):
    with make_client(schema, root_path="/tns", rate_limit_per_minute=2) as client:
        codes = [client.get("/tns/healthz").status_code for _ in range(6)]
    assert codes == [200] * 6
