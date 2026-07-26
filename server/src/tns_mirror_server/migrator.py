"""Forward-only SQL migrations, without an ORM.

Design §8. The mirror is a headless sync job; a full migration framework would be
more attack surface than the problem deserves. This runner does exactly four
things: find numbered ``.sql`` files, apply the ones not yet applied, record them
with a checksum, and refuse to continue if a file changed after it was applied.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

from psycopg import sql

from .config import _require_identifier
from .errors import MigrationError

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

log = logging.getLogger(__name__)

#: Namespaced so it cannot collide with another application's advisory locks.
_ADVISORY_LOCK_KEY = 0x746E_736D  # 'tnsm'

_SCHEMA_PACKAGE = "tns_mirror_server.schema"


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    template: str

    @property
    def checksum(self) -> str:
        """Digest of the *template*, before identifier substitution.

        Checksumming the template rather than the rendered SQL means changing
        ``TNS_TABLE`` does not masquerade as an edited migration.
        """
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()

    def render(self, schema: str, table: str) -> str:
        # Both names are re-validated here, not merely at config load: this is
        # the point where they become DDL, so this is where being wrong matters.
        _require_identifier(schema, "database.schema")
        _require_identifier(table, "database.table")
        return self.template.replace("{{schema}}", schema).replace("{{table}}", table)


def load_migrations() -> list[Migration]:
    """Every packaged migration, ordered by version."""
    files = [
        entry
        for entry in resources.files(_SCHEMA_PACKAGE).iterdir()
        if entry.name.endswith(".sql")
    ]
    if not files:  # pragma: no cover - packaging failure
        raise MigrationError(f"no migrations packaged under {_SCHEMA_PACKAGE}")
    return [
        Migration(version=entry.name.removesuffix(".sql"), template=entry.read_text("utf-8"))
        for entry in sorted(files, key=lambda e: e.name)
    ]


class Migrator:
    """Applies migrations to one database, under an advisory lock."""

    def __init__(self, conn: psycopg.Connection, *, schema: str, table: str) -> None:
        self._conn = conn
        self._schema = _require_identifier(schema, "database.schema")
        self._table = _require_identifier(table, "database.table")

    # SQL has no parameter form for an identifier, so config-supplied names are
    # composed through psycopg's Identifier, which quotes and escapes them. The
    # regex validation above is defence in depth and a better error message, not
    # the thing that makes this safe.
    @property
    def _bookkeeping(self) -> sql.Identifier:
        return sql.Identifier(self._schema, "schema_migrations")

    def _ensure_bookkeeping(self) -> None:
        self._conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self._schema))
        )
        self._conn.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {table} (
                    version    text        PRIMARY KEY,
                    checksum   text        NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            ).format(table=self._bookkeeping)
        )

    def applied(self) -> dict[str, str]:
        """Version -> checksum for everything already applied."""
        self._ensure_bookkeeping()
        rows = self._conn.execute(
            sql.SQL("SELECT version, checksum FROM {table}").format(table=self._bookkeeping)
        ).fetchall()
        return dict(rows)

    def apply(self) -> list[str]:
        """Apply pending migrations. Returns the versions applied, oldest first."""
        migrations = load_migrations()
        applied_now: list[str] = []

        with self._conn.transaction():
            # Serialise against another replica starting up at the same moment.
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            already = self.applied()

            for migration in migrations:
                recorded = already.get(migration.version)
                if recorded is not None:
                    if recorded != migration.checksum:
                        raise MigrationError(
                            f"migration {migration.version} was modified after it was "
                            f"applied (checksum {recorded[:12]}… on disk "
                            f"{migration.checksum[:12]}…). Migrations are forward-only: "
                            f"add a new one instead of editing a released file."
                        )
                    continue

                log.info("applying migration %s", migration.version)
                self._conn.execute(migration.render(self._schema, self._table))
                self._conn.execute(
                    sql.SQL("INSERT INTO {table} (version, checksum) VALUES (%s, %s)").format(
                        table=self._bookkeeping
                    ),
                    (migration.version, migration.checksum),
                )
                applied_now.append(migration.version)

        return applied_now
