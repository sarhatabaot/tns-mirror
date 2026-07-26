"""Store integration tests against a real Postgres.

Skipped unless ``TNS_TEST_DSN`` points at a database the test user may create
schemas in. Run with::

    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pg --name tns-pg postgres:16
    TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration

Each test gets its own Postgres schema, so they are isolated and the
``TNS_SCHEMA`` setting is exercised at the same time.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import psycopg
import pytest

from conftest import make_config
from tns_mirror_server.adapters.store_postgres import PostgresStore
from tns_mirror_server.errors import MigrationError
from tns_mirror_server.ports.source import TnsRecord

pytestmark = pytest.mark.integration

DSN = os.environ.get("TNS_TEST_DSN")

if not DSN:  # pragma: no cover - environment dependent
    pytest.skip("set TNS_TEST_DSN to run store integration tests", allow_module_level=True)


def record(objid, name, ra=1.0, dec=2.0, **kwargs) -> TnsRecord:
    return TnsRecord(objid=objid, name=name, ra=ra, dec=dec, **kwargs)


@pytest.fixture
def config(tmp_path):
    schema = f"tns_test_{uuid.uuid4().hex[:12]}"
    cfg = make_config(tmp_path, dsn=DSN, schema=schema)
    yield cfg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture
def store(config):
    with PostgresStore.connect(config) as store:
        store.migrate()
        yield store


# --- migrations -------------------------------------------------------------


def test_migrate_creates_the_contract_and_is_idempotent(config):
    with PostgresStore.connect(config) as store:
        assert store.migrate() == ["0001_initial"]
        assert store.migrate() == []  # nothing pending the second time
        assert store.count() == 0
        assert store.meta().schema_version == 1


def test_migrate_creates_the_documented_indexes(config, store):
    with psycopg.connect(DSN, autocommit=True) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                (config.database.schema,),
            ).fetchall()
        }

    assert "tns_objects_ra_dec_idx" in names
    assert "tns_objects_name_idx" in names


def test_a_migration_edited_after_release_is_refused(config, store):
    # Forward-only means forward-only: silently re-running an altered file would
    # leave two deployments claiming the same schema version with different shapes.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {config.database.schema}.schema_migrations SET checksum = 'tampered'"
        )

    with (
        PostgresStore.connect(config) as fresh,
        pytest.raises(MigrationError, match="modified"),
    ):
        fresh.migrate()


def test_a_custom_table_name_is_honoured(tmp_path):
    schema = f"tns_test_{uuid.uuid4().hex[:12]}"
    cfg = make_config(tmp_path, dsn=DSN, schema=schema, table="catalogue")
    try:
        with PostgresStore.connect(cfg) as store:
            store.migrate()
            store.replace([record(1, "AT1")], snapshot=date(2026, 7, 26))
            assert store.count() == 1
        with psycopg.connect(DSN, autocommit=True) as conn:
            assert conn.execute(f"SELECT count(*) FROM {schema}.catalogue").fetchone()[0] == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


# --- the full snapshot ------------------------------------------------------


def test_replace_swaps_the_catalogue_and_drops_depublished_objects(store):
    store.replace([record(1, "AT1"), record(999, "ATgone")], snapshot=date(2026, 7, 25))
    store.replace([record(1, "AT1"), record(2, "SN2")], snapshot=date(2026, 7, 26))

    assert store.count() == 2
    meta = store.meta()
    assert meta.last_source_snapshot == date(2026, 7, 26)
    assert meta.last_full_sync_at is not None


def test_replace_is_atomic_to_a_concurrent_reader(config, store):
    """Invariant 3: a reader never sees a half-written catalogue."""
    store.replace(
        [record(objid, f"AT{objid}") for objid in range(10)], snapshot=date(2026, 7, 25)
    )
    observed: list[int] = []

    def slow_records():
        # Another connection peeks partway through the swap. Because the DELETE
        # and the INSERTs share one uncommitted transaction, MVCC must still show
        # it the *previous* catalogue in full.
        yield record(100, "AT100")
        with psycopg.connect(DSN, autocommit=True) as peek:
            table = f"{config.database.schema}.{config.database.table}"
            observed.append(peek.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        yield record(101, "AT101")

    store.replace(slow_records(), snapshot=date(2026, 7, 26))

    assert observed == [10], "reader saw the swap in progress"
    assert store.count() == 2


def test_duplicate_objids_in_one_file_do_not_abort_the_refresh(config, store):
    # TNS should not ship these, but the whole snapshot commits as one
    # transaction: a single duplicate must not cost the entire refresh.
    # Last occurrence wins, matching the oldest-to-newest rule used elsewhere.
    store.replace(
        [record(1, "first"), record(2, "SN2"), record(1, "last")], snapshot=date(2026, 7, 26)
    )

    assert store.count() == 2
    table = f"{config.database.schema}.{config.database.table}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        name = conn.execute(f"SELECT name FROM {table} WHERE objid = 1").fetchone()[0]
    assert name == "last"


def test_every_contract_column_round_trips(config, store):
    discovered = datetime(2026, 7, 1, 3, 22, 11, tzinfo=UTC)
    store.replace(
        [
            record(
                1,
                "SN2026xyz",
                ra=203.1,
                dec=-45.25,
                type="SN Ia",
                redshift=0.031,
                discoverydate=discovered,
                discoverymag=18.1,
                internal_names="ZTF26aaa,ATLAS26x",
                reporting_group="ZTF",
            )
        ],
        snapshot=date(2026, 7, 26),
    )

    table = f"{config.database.schema}.{config.database.table}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute(
            f'SELECT objid, name, ra, "dec", "type", redshift, discoverydate, '
            f"discoverymag, internal_names, reporting_group, source_snapshot, "
            f"refreshed_at FROM {table}"
        ).fetchone()

    assert row[0] == 1
    assert row[1] == "SN2026xyz"
    assert row[2] == pytest.approx(203.1)
    assert row[3] == pytest.approx(-45.25)
    assert row[4] == "SN Ia"
    assert row[5] == pytest.approx(0.031)
    assert row[6] == discovered
    assert row[7] == pytest.approx(18.1)
    assert row[8] == "ZTF26aaa,ATLAS26x"
    assert row[9] == "ZTF"
    assert row[10] == date(2026, 7, 26)
    assert row[11] is not None


def test_replace_handles_more_rows_than_one_batch(store, monkeypatch):
    monkeypatch.setattr("tns_mirror_server.adapters.store_postgres.BATCH_SIZE", 100)
    store.replace(
        (record(objid, f"AT{objid}") for objid in range(250)), snapshot=date(2026, 7, 26)
    )
    assert store.count() == 250


# --- deltas -----------------------------------------------------------------


def test_upsert_updates_existing_rows_and_inserts_new_ones(config, store):
    store.replace([record(1, "AT1"), record(2, "SN2")], snapshot=date(2026, 7, 25))

    written = store.upsert(
        [record(1, "AT1renamed"), record(3, "AT3")], snapshot=date(2026, 7, 26)
    )

    assert written == 2
    table = f"{config.database.schema}.{config.database.table}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = dict(conn.execute(f"SELECT objid, name FROM {table}").fetchall())
    assert rows == {1: "AT1renamed", 2: "SN2", 3: "AT3"}


def test_upsert_is_idempotent(config, store):
    delta = [record(1, "AT1"), record(2, "SN2")]
    store.upsert(delta, snapshot=date(2026, 7, 26))
    store.upsert(delta, snapshot=date(2026, 7, 26))

    assert store.count() == 2


def test_a_delta_stamps_freshness_only_when_it_wrote_something(store):
    assert store.upsert([], snapshot=date(2026, 7, 26)) == 0
    assert store.meta().last_delta_sync_at is None

    store.upsert([record(1, "AT1")], snapshot=date(2026, 7, 26))
    assert store.meta().last_delta_sync_at is not None


def test_a_delta_can_insert_an_object_no_snapshot_has_seen(config, store):
    # source_snapshot is NOT NULL, so a delta-only object still needs a value:
    # it is the date of the file that wrote the row, not of a daily snapshot.
    store.upsert([record(7, "AT7")], snapshot=date(2026, 7, 26))

    table = f"{config.database.schema}.{config.database.table}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        snapshot = conn.execute(f"SELECT source_snapshot FROM {table}").fetchone()[0]
    assert snapshot == date(2026, 7, 26)


# --- the consumer role ------------------------------------------------------


def test_the_printed_grants_produce_a_reader_that_cannot_write(config, store):
    """The read-only role is the privilege boundary; verify it actually holds."""
    schema, table = config.database.schema, config.database.table
    role = f"tns_ro_{uuid.uuid4().hex[:8]}"
    store.replace([record(1, "AT1")], snapshot=date(2026, 7, 26))

    admin = psycopg.connect(DSN, autocommit=True)
    try:
        try:
            admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'test-only-password'")
        except psycopg.errors.InsufficientPrivilege:  # pragma: no cover
            pytest.skip("test user cannot create roles")

        current_db = admin.execute("SELECT current_database()").fetchone()[0]
        admin.execute(f"GRANT CONNECT ON DATABASE {current_db} TO {role}")
        admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        admin.execute(f"GRANT SELECT ON {schema}.{table} TO {role}")
        admin.execute(f"GRANT SELECT ON {schema}.tns_mirror_meta TO {role}")

        reader_dsn = psycopg.conninfo.make_conninfo(
            DSN, user=role, password="test-only-password", dbname=current_db
        )
        with psycopg.connect(reader_dsn, autocommit=True) as reader:
            assert reader.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0] == 1
            assert (
                reader.execute(
                    f"SELECT schema_version FROM {schema}.tns_mirror_meta"
                ).fetchone()[0]
                == 1
            )

            for statement in (
                f'INSERT INTO {schema}.{table} (objid, name, ra, "dec", source_snapshot, '
                f"refreshed_at) VALUES (2, 'x', 1, 1, current_date, now())",
                f"UPDATE {schema}.{table} SET name = 'x'",
                f"DELETE FROM {schema}.{table}",
                f"DROP TABLE {schema}.{table}",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute(statement)
    finally:
        admin.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER")
        admin.execute(f"DROP OWNED BY {role}")
        admin.execute(f"DROP ROLE IF EXISTS {role}")
        admin.close()
