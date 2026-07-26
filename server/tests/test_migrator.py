"""Migration loading, rendering, and the edited-after-apply guard.

The parts that need a live database are in ``test_store_postgres.py``.
"""

from __future__ import annotations

import pytest

from tns_mirror_server.errors import ConfigError
from tns_mirror_server.migrator import Migration, load_migrations


def test_the_initial_migration_is_packaged():
    # It ships inside the package so the image needs no files from outside it.
    migrations = load_migrations()

    assert [m.version for m in migrations] == ["0001_initial"]
    assert "CREATE TABLE IF NOT EXISTS" in migrations[0].template


def test_migrations_are_ordered_by_version():
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)


def test_rendering_substitutes_schema_and_table():
    migration = load_migrations()[0]

    rendered = migration.render("astro", "tns_objects")

    assert "astro.tns_objects" in rendered
    assert "{{schema}}" not in rendered
    assert "{{table}}" not in rendered
    # Index names are composed from the bare table name, so they must render as
    # valid identifiers rather than picking up quoting.
    assert "tns_objects_ra_dec_idx" in rendered


def test_rendering_rejects_names_that_are_not_identifiers():
    migration = load_migrations()[0]

    with pytest.raises(ConfigError, match="identifier"):
        migration.render("public", 'tns"; DROP TABLE users; --')


def test_checksum_covers_the_template_not_the_rendering():
    # Otherwise changing TNS_TABLE would look like an edited migration and the
    # forward-only guard would fire on a legitimate config change.
    migration = load_migrations()[0]
    other = Migration(version=migration.version, template=migration.template)

    assert migration.checksum == other.checksum
    assert Migration("x", migration.template + "\n-- edit").checksum != migration.checksum


def test_the_contract_declares_every_documented_column():
    template = load_migrations()[0].template

    for column in (
        "objid",
        "name",
        "ra",
        '"dec"',
        '"type"',
        "redshift",
        "discoverydate",
        "discoverymag",
        "internal_names",
        "reporting_group",
        "source_snapshot",
        "refreshed_at",
    ):
        assert column in template, f"{column} missing from the published schema"


def test_metadata_table_is_part_of_the_contract():
    # Consumers need it to assert the schema major they were built against and to
    # judge freshness; it is granted to the read-only role alongside the catalogue.
    template = load_migrations()[0].template

    assert "tns_mirror_meta" in template
    assert "schema_version" in template
    assert "last_full_sync_at" in template
