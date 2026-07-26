"""The contract test: the published schema and this client must agree.

The server and the client share no code — after the server is extracted to its
own repository they *cannot*. The only thing binding them is
``schema/schema_v1.sql``, so this reads that file and checks the client's model
against it. It needs no database, so it runs on every commit.

If this fails, either the schema changed without the client following, or the
client invented a column. Both are contract breaks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tns_mirror_client import SCHEMA_VERSION
from tns_mirror_client.models import CATALOGUE_COLUMNS, MirrorMeta, TnsObject

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema" / f"schema_v{SCHEMA_VERSION}.sql"

# Column definitions inside a CREATE TABLE body: an optionally-quoted name
# followed by a type. Deliberately simple — if the DDL grows past what this
# parses, the test should be rewritten rather than made clever.
_COLUMN = re.compile(
    r'^\s*"?([a-z_]+)"?\s+(bigint|text|double precision|timestamptz|date|integer|smallint)\b'
)


def table_columns(sql: str, table: str) -> list[str]:
    """Column names declared in ``CREATE TABLE ... table (...)``."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS\s+\S*{re.escape(table)}\s*\((.*?)\n\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise AssertionError(f"no CREATE TABLE for {table} in {SCHEMA_FILE}")

    columns = []
    for line in match.group(1).splitlines():
        if line.strip().startswith(("--", "CONSTRAINT", "PRIMARY KEY", "UNIQUE")):
            continue
        found = _COLUMN.match(line)
        if found:
            columns.append(found.group(1))
    return columns


@pytest.fixture(scope="module")
def schema_sql() -> str:
    if not SCHEMA_FILE.exists():  # pragma: no cover - repo layout guard
        pytest.fail(
            f"published contract missing at {SCHEMA_FILE}. Run "
            f"'python server/scripts/render_schema.py'."
        )
    return SCHEMA_FILE.read_text(encoding="utf-8")


def test_the_model_matches_the_published_catalogue_exactly(schema_sql):
    published = table_columns(schema_sql, "tns_objects")

    assert list(CATALOGUE_COLUMNS) == published, (
        "TnsObject and the published schema have diverged.\n"
        f"  schema: {published}\n"
        f"  model:  {list(CATALOGUE_COLUMNS)}"
    )


def test_column_order_is_preserved(schema_sql):
    # Not cosmetic: the client builds its SELECT from CATALOGUE_COLUMNS, and
    # matching the contract's order keeps generated SQL readable next to the DDL.
    published = table_columns(schema_sql, "tns_objects")
    assert published[0] == "objid", "objid is the primary key and leads the contract"
    assert published[-2:] == ["source_snapshot", "refreshed_at"]


def test_the_meta_model_matches_the_published_meta_table(schema_sql):
    published = [c for c in table_columns(schema_sql, "tns_mirror_meta") if c != "id"]
    modelled = list(MirrorMeta.__dataclass_fields__)

    assert modelled == published


def test_separation_is_derived_not_stored(schema_sql):
    # It is computed per query, so it must not appear in the contract — and it
    # must not be in the SELECT the client builds.
    published = table_columns(schema_sql, "tns_objects")

    assert "separation_arcsec" not in published
    assert "separation_arcsec" not in CATALOGUE_COLUMNS
    assert "separation_arcsec" in TnsObject.__dataclass_fields__


def test_the_client_major_matches_the_published_schema_version(schema_sql):
    from tns_mirror_client import __version__

    major = int(__version__.split(".")[0])
    assert major == SCHEMA_VERSION, (
        f"client {__version__} must speak schema v{major}; the contract is "
        f"v{SCHEMA_VERSION}. The majors move together."
    )
    assert f"version {SCHEMA_VERSION}" in schema_sql


def test_nullability_matches_what_the_model_claims(schema_sql):
    """The six NOT NULL columns are the ones the model types as non-optional."""
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+\S*tns_objects\s*\((.*?)\n\);",
        schema_sql,
        re.DOTALL,
    ).group(1)

    not_null = {
        _COLUMN.match(line).group(1)
        for line in body.splitlines()
        if _COLUMN.match(line) and ("NOT NULL" in line or "PRIMARY KEY" in line)
    }

    assert not_null == {"objid", "name", "ra", "dec", "source_snapshot", "refreshed_at"}

    for column in not_null:
        annotation = TnsObject.__annotations__[column]
        assert "None" not in str(annotation), (
            f"{column} is NOT NULL in the contract but optional in the model"
        )
