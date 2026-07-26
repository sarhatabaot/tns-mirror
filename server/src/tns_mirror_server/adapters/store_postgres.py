"""Postgres implementation of the ``Store`` port.

Design §4 and A.3. This module is the only place in the service that knows SQL or
the table name — everything above it speaks :class:`TnsRecord`.

Invariant 3 rests on :meth:`replace` being a *single* transaction: the DELETE and
every INSERT commit together, so MVCC hides the swap and a concurrent reader sees
either the old catalogue or the new one, never a partial one. Do not split it.

Every identifier that comes from config is composed through psycopg's ``sql``
module rather than formatted into a string. SQL has no parameter form for an
identifier, and quoting one correctly is not a thing to hand-roll.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from itertools import islice
from typing import Final

import psycopg
from psycopg import sql

from ..config import Config, _require_identifier
from ..errors import ConfigError
from ..migrator import Migrator
from ..ports.source import TnsRecord
from ..ports.store import MirrorMeta

log = logging.getLogger(__name__)

#: Rows per executemany round trip. Large enough to amortise the round trip,
#: small enough that a batch's parameter list stays comfortably in memory.
BATCH_SIZE: Final = 5_000

#: The catalogue columns, in contract order. 'dec' and 'type' need no special
#: handling here — Identifier quotes every name it renders.
_COLUMNS: Final = (
    "objid",
    "name",
    "ra",
    "dec",
    "type",
    "redshift",
    "discoverydate",
    "discoverymag",
    "internal_names",
    "reporting_group",
)


def _chunked(iterable: Iterable[TnsRecord], size: int) -> Iterator[list[TnsRecord]]:
    iterator = iter(iterable)
    while batch := list(islice(iterator, size)):
        yield batch


class PostgresStore:
    """Owns the mirror's database. Holds *write* credentials; consumers never do."""

    def __init__(self, conn: psycopg.Connection, *, schema: str, table: str) -> None:
        self._conn = conn
        self._schema = _require_identifier(schema, "database.schema")
        self._table = _require_identifier(table, "database.table")

    # --- lifecycle ----------------------------------------------------------

    @classmethod
    @contextmanager
    def connect(cls, config: Config) -> Iterator[PostgresStore]:
        """Open a connection for the duration of one command.

        ``autocommit=True`` with explicit ``transaction()`` blocks is psycopg 3's
        recommended shape: it keeps transaction boundaries visible in the code
        instead of implied by an idle-in-transaction connection.
        """
        dsn = config.database.dsn
        try:
            # An empty DSN is intentional: libpq then reads PGHOST/PGUSER/... .
            conn = (
                psycopg.connect(dsn, autocommit=True)
                if dsn
                else psycopg.connect(autocommit=True)
            )
        except psycopg.OperationalError as exc:
            raise ConfigError(
                "could not connect to the mirror database. Set DATABASE_URL (or the "
                f"standard PG* variables). Underlying error: {exc}"
            ) from exc
        try:
            yield cls(conn, schema=config.database.schema, table=config.database.table)
        finally:
            conn.close()

    # --- identifiers --------------------------------------------------------

    @property
    def _catalogue(self) -> sql.Identifier:
        return sql.Identifier(self._schema, self._table)

    @property
    def _meta_table(self) -> sql.Identifier:
        return sql.Identifier(self._schema, "tns_mirror_meta")

    # --- schema -------------------------------------------------------------

    def migrate(self) -> list[str]:
        return Migrator(self._conn, schema=self._schema, table=self._table).apply()

    # --- writes -------------------------------------------------------------

    def _insert_sql(self) -> sql.Composed:
        assignments = sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(name))
            for name in _COLUMNS
            if name != "objid"
        )
        # ON CONFLICT on the full-snapshot path too: the DELETE means the only
        # possible conflict is a duplicate objid *within the same file*, and
        # last-one-wins is far better than aborting an entire refresh over it.
        return sql.SQL(
            "INSERT INTO {table} ({columns}, source_snapshot, refreshed_at) "
            "VALUES ({values}, {snapshot}, now()) "
            "ON CONFLICT (objid) DO UPDATE SET {assignments}, "
            "source_snapshot = EXCLUDED.source_snapshot, refreshed_at = now()"
        ).format(
            table=self._catalogue,
            columns=sql.SQL(", ").join(sql.Identifier(name) for name in _COLUMNS),
            values=sql.SQL(", ").join(sql.Placeholder(name) for name in _COLUMNS),
            snapshot=sql.Placeholder("source_snapshot"),
            assignments=assignments,
        )

    def _write_batches(
        self, cur: psycopg.Cursor, records: Iterable[TnsRecord], snapshot: date
    ) -> int:
        statement = self._insert_sql()
        written = 0
        for batch in _chunked(records, BATCH_SIZE):
            params = [{**record.as_row(), "source_snapshot": snapshot} for record in batch]
            cur.executemany(statement, params)
            written += len(batch)
            log.debug("wrote %d rows (%d total)", len(batch), written)
        return written

    def replace(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        """Swap the whole catalogue atomically (invariant 3)."""
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {}").format(self._catalogue))
            written = self._write_batches(cur, records, snapshot)
            # Freshness commits with the data, so it can never claim a sync that
            # rolled back.
            cur.execute(
                sql.SQL(
                    "UPDATE {table} SET last_full_sync_at = now(), "
                    "last_source_snapshot = %s WHERE id = 1"
                ).format(table=self._meta_table),
                (snapshot,),
            )
        log.info("full snapshot committed: %d rows (snapshot %s)", written, snapshot)
        return written

    def upsert(self, records: Iterable[TnsRecord], *, snapshot: date) -> int:
        """Merge a delta on ``objid``. Idempotent — replaying an hour is a no-op."""
        with self._conn.transaction(), self._conn.cursor() as cur:
            written = self._write_batches(cur, records, snapshot)
            if written:
                cur.execute(
                    sql.SQL(
                        "UPDATE {table} SET last_delta_sync_at = now() WHERE id = 1"
                    ).format(table=self._meta_table)
                )
        return written

    # --- reads (operational only; consumer queries belong to the client) -----

    def count(self) -> int:
        row = self._conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(self._catalogue)
        ).fetchone()
        return int(row[0]) if row else 0

    def meta(self) -> MirrorMeta:
        row = self._conn.execute(
            sql.SQL(
                "SELECT schema_version, last_full_sync_at, last_delta_sync_at, "
                "last_source_snapshot FROM {table} WHERE id = 1"
            ).format(table=self._meta_table)
        ).fetchone()
        if row is None:
            raise ConfigError(
                f"{self._schema}.tns_mirror_meta has no row — run "
                f"'tns-mirror-server migrate' before syncing."
            )
        return MirrorMeta(
            schema_version=row[0],
            last_full_sync_at=row[1],
            last_delta_sync_at=row[2],
            last_source_snapshot=row[3],
        )
