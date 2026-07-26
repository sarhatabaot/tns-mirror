"""The read client.

Connects with read-only credentials and returns typed results. It never writes,
carries no TNS credentials — those are a *server* concern — and shares no code
with the server. The only thing the two have in common is the schema.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .errors import MirrorUnavailable, SchemaVersionError
from .geometry import bounding_box
from .models import CATALOGUE_COLUMNS, MirrorMeta, TnsObject

log = logging.getLogger(__name__)

__all__ = ["SCHEMA_VERSION", "TnsMirror"]

#: The contract major this client speaks. Client 1.x ⇄ schema v1.
SCHEMA_VERSION = 1

_ARCSEC_PER_DEG = 3600.0

# Great-circle separation in degrees, by the haversine formula — the same one
# geometry.angular_separation_deg computes in Python, so the two cannot drift.
#
# Not the law of cosines. Its argument approaches 1 as the separation approaches
# zero, where acos loses most of its significant digits: an object matched
# against itself comes back at ~3 milliarcseconds rather than 0. Small
# separations are the entire point of a cross-match, so the formula has to be
# accurate there. The clamp guards asin's domain for near-antipodal rows.
_SEPARATION_SQL = """
    2 * degrees(asin(least(1, sqrt(
        power(sin((radians({dec_col}) - radians(%(dec)s)) / 2), 2)
      + cos(radians(%(dec)s)) * cos(radians({dec_col}))
      * power(sin((radians({ra_col}) - radians(%(ra)s)) / 2), 2)
    ))))
"""


class TnsMirror:
    """A read-only view of a tns-mirror database.

    >>> tns = TnsMirror(dsn=os.environ["TNS_RO_DSN"])
    >>> hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
    >>> if hit:
    ...     print(hit.name, hit.type, hit.redshift)

    Use it as a context manager, or call :meth:`close`, to release the
    connection. The connection is opened lazily on first use.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: psycopg.Connection | None = None,
        schema: str = "public",
        table: str = "tns_objects",
        check_schema_version: bool = True,
    ) -> None:
        """
        ``dsn`` is a read-only connection string. Pass ``connection`` instead to
        reuse one you already manage (from a pool, say) — the client will not
        close what it did not open.

        ``schema`` and ``table`` only need setting if the mirror was deployed
        with non-default ``TNS_SCHEMA``/``TNS_TABLE``.
        """
        if dsn is None and connection is None:
            raise ValueError("TnsMirror needs either a dsn or a connection")

        self._dsn = dsn
        self._conn = connection
        self._owns_connection = connection is None
        self._schema = schema
        self._table = table
        self._check_version = check_schema_version
        self._verified = False

    # --- connection ---------------------------------------------------------

    @property
    def _catalogue(self) -> sql.Identifier:
        return sql.Identifier(self._schema, self._table)

    @property
    def _meta_table(self) -> sql.Identifier:
        return sql.Identifier(self._schema, "tns_mirror_meta")

    def _connect(self) -> psycopg.Connection:
        if self._conn is None:
            try:
                self._conn = psycopg.connect(self._dsn, autocommit=True)
            except psycopg.OperationalError as exc:
                raise MirrorUnavailable(f"could not connect to the mirror: {exc}") from exc

            # Defence in depth: this library has no write path, and now the
            # session has no write capability either, whatever the role allows.
            if self._owns_connection:
                self._conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

        if self._check_version and not self._verified:
            try:
                self._verify_schema_version()
            except Exception:
                # The caller never got a usable client, so it has nothing to
                # close — leaking the socket here would be ours, not theirs.
                self.close()
                raise

        return self._conn

    def _verify_schema_version(self) -> None:
        """Fail loudly at connect time rather than quietly at query time."""
        found = self.meta(_skip_verification=True).schema_version
        if found != SCHEMA_VERSION:
            raise SchemaVersionError(found=found, expected=SCHEMA_VERSION)
        self._verified = True

    @contextmanager
    def _cursor(self) -> Iterator[psycopg.Cursor]:
        conn = self._connect()
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur

    def close(self) -> None:
        """Close the connection, if this client opened it."""
        if self._conn is not None and self._owns_connection:
            self._conn.close()
            self._conn = None
            self._verified = False

    def __enter__(self) -> TnsMirror:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- queries ------------------------------------------------------------

    def _columns(self) -> sql.Composed:
        return sql.SQL(", ").join(sql.Identifier(name) for name in CATALOGUE_COLUMNS)

    def nearest(self, ra: float, dec: float, radius_arcsec: float = 3.0) -> TnsObject | None:
        """The closest object within ``radius_arcsec``, or ``None``.

        This is the cross-match every consumer needs and the reason to use a
        library rather than hand-written SQL: the bounding-box prefilter, the
        pole and 0/360-seam handling, and the clamped great-circle test are all
        easy to get subtly wrong.

        Ties break on ``objid``, so the result is deterministic when two objects
        sit at identical separation.
        """
        matches = self.search(ra, dec, radius_arcsec, limit=1)
        return matches[0] if matches else None

    def search(
        self,
        ra: float,
        dec: float,
        radius_arcsec: float = 3.0,
        *,
        limit: int | None = None,
    ) -> list[TnsObject]:
        """Every object within ``radius_arcsec``, nearest first.

        Each result carries its ``separation_arcsec``.
        """
        if radius_arcsec < 0:
            raise ValueError(f"radius_arcsec must not be negative, got {radius_arcsec}")
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        radius_deg = radius_arcsec / _ARCSEC_PER_DEG
        box = bounding_box(ra, dec, radius_deg)

        params: dict[str, Any] = {
            "ra": ra,
            "dec": dec,
            "radius_deg": radius_deg,
            "dec_min": box.dec_min,
            "dec_max": box.dec_max,
        }

        # The prefilter is what makes this use the (ra, dec) index instead of
        # computing a great-circle distance for every row in the catalogue.
        conditions = [sql.SQL("{} BETWEEN %(dec_min)s AND %(dec_max)s").format(_dec_col())]
        if box.constrains_ra:
            ra_clauses = []
            for index, (low, high) in enumerate(box.ra_ranges):
                params[f"ra_min_{index}"] = low
                params[f"ra_max_{index}"] = high
                ra_clauses.append(
                    sql.SQL("{col} BETWEEN %(ra_min_{i})s AND %(ra_max_{i})s").format(
                        col=_ra_col(), i=sql.SQL(str(index))
                    )
                )
            conditions.append(sql.SQL("({})").format(sql.SQL(" OR ").join(ra_clauses)))

        separation = sql.SQL(_SEPARATION_SQL).format(dec_col=_dec_col(), ra_col=_ra_col())

        # Wrapped in a subquery because SQL cannot reference an output alias in
        # WHERE, and repeating the whole trig expression there invites drift.
        statement = sql.SQL(
            "SELECT * FROM ("
            "  SELECT {columns}, {separation} AS separation_deg"
            "  FROM {table} WHERE {conditions}"
            ") AS candidates "
            "WHERE separation_deg <= %(radius_deg)s "
            "ORDER BY separation_deg, objid"
        ).format(
            columns=self._columns(),
            separation=separation,
            table=self._catalogue,
            conditions=sql.SQL(" AND ").join(conditions),
        )

        if limit is not None:
            params["limit"] = limit
            statement = sql.SQL("{} LIMIT %(limit)s").format(statement)

        with self._cursor() as cur:
            cur.execute(statement, params)
            return [_to_object(row, with_separation=True) for row in cur.fetchall()]

    def by_name(self, name: str) -> TnsObject | None:
        """Look up one object by its IAU name, e.g. ``AT2026abc``.

        ``name`` is not unique-constrained in the schema — a delta may carry a
        rename that transiently collides — so this orders by ``objid`` and
        returns the first, rather than pretending the case cannot arise.
        """
        statement = sql.SQL(
            "SELECT {columns} FROM {table} WHERE name = %(name)s ORDER BY objid LIMIT 1"
        ).format(columns=self._columns(), table=self._catalogue)

        with self._cursor() as cur:
            cur.execute(statement, {"name": name})
            row = cur.fetchone()
        return _to_object(row) if row else None

    def by_objid(self, objid: int) -> TnsObject | None:
        """Look up one object by its stable TNS identifier (the primary key)."""
        statement = sql.SQL("SELECT {columns} FROM {table} WHERE objid = %(objid)s").format(
            columns=self._columns(), table=self._catalogue
        )

        with self._cursor() as cur:
            cur.execute(statement, {"objid": objid})
            row = cur.fetchone()
        return _to_object(row) if row else None

    def by_names(self, names: Sequence[str]) -> dict[str, TnsObject]:
        """Look up many objects at once, keyed by name.

        One round trip instead of N — worth having when annotating a candidate
        list. Names with no match are simply absent from the result.
        """
        if not names:
            return {}

        statement = sql.SQL(
            "SELECT {columns} FROM {table} WHERE name = ANY(%(names)s) ORDER BY objid"
        ).format(columns=self._columns(), table=self._catalogue)

        with self._cursor() as cur:
            cur.execute(statement, {"names": list(names)})
            rows = cur.fetchall()
        return {row["name"]: _to_object(row) for row in rows}

    def count(self) -> int:
        """How many objects the mirror currently holds."""
        statement = sql.SQL("SELECT count(*) AS n FROM {}").format(self._catalogue)
        with self._cursor() as cur:
            cur.execute(statement)
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def meta(self, *, _skip_verification: bool = False) -> MirrorMeta:
        """The contract version and how fresh the data is.

        Check :meth:`MirrorMeta.is_fresh` before trusting a batch job: a row
        count cannot tell you whether the mirror stopped syncing weeks ago.
        """
        statement = sql.SQL(
            "SELECT schema_version, last_full_sync_at, last_delta_sync_at, "
            "last_source_snapshot FROM {} WHERE id = 1"
        ).format(self._meta_table)

        conn = self._conn if _skip_verification else self._connect()
        if conn is None:  # pragma: no cover - only reachable via misuse
            raise MirrorUnavailable("no connection")

        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(statement)
                row = cur.fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise MirrorUnavailable(
                "the read-only role cannot see tns_mirror_meta. Re-run "
                "'tns-mirror-server print-grants' — the client needs it to check "
                "the schema version it was built against."
            ) from exc
        except psycopg.errors.UndefinedTable as exc:
            raise MirrorUnavailable(
                "this database has no tns_mirror_meta table — it does not look "
                "like a tns-mirror database, or migrations have not been run."
            ) from exc

        if row is None:
            raise MirrorUnavailable("tns_mirror_meta is empty; run 'tns-mirror-server migrate'")

        return MirrorMeta(
            schema_version=row["schema_version"],
            last_full_sync_at=row["last_full_sync_at"],
            last_delta_sync_at=row["last_delta_sync_at"],
            last_source_snapshot=row["last_source_snapshot"],
        )


def _ra_col() -> sql.Identifier:
    return sql.Identifier("ra")


def _dec_col() -> sql.Identifier:
    return sql.Identifier("dec")


def _to_object(row: dict[str, Any], *, with_separation: bool = False) -> TnsObject:
    separation = None
    if with_separation and row.get("separation_deg") is not None:
        separation = row["separation_deg"] * _ARCSEC_PER_DEG

    return TnsObject(
        **{name: row[name] for name in CATALOGUE_COLUMNS},
        separation_arcsec=separation,
    )
