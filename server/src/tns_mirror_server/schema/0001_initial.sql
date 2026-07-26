-- tns-mirror schema v1 — the published consumer contract.
--
-- Forward-only. Once released, this file is FROZEN: a breaking change (rename or
-- drop a column, change a type or unit) is a new major and a new migration, never
-- an edit here. See schema/README.md for the versioning policy.
--
-- The schema and table names below are substituted by the migration runner from
-- config (TNS_SCHEMA / TNS_TABLE), and validated as plain SQL identifiers first.
-- The published contract in schema/schema_v1.sql is this file at its defaults.

CREATE SCHEMA IF NOT EXISTS {{schema}};

-- ---------------------------------------------------------------------------
-- The catalogue. One row per TNS object.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {{schema}}.{{table}} (
    -- Stable TNS identifier. Primary key, and the conflict target every hourly
    -- delta upserts on. Rows arriving without one are unaddressable and dropped.
    objid            bigint            PRIMARY KEY,

    -- IAU name, name_prefix and name joined: 'AT2026abc', 'SN2026xyz'.
    -- Expected unique in practice but deliberately NOT constrained unique: a
    -- delta may carry a rename that transiently collides with a not-yet-updated
    -- row, and aborting a legitimate delta is worse than a momentary duplicate.
    name             text              NOT NULL,

    -- J2000, degrees. ra in [0,360), dec in [-90,90].
    ra               double precision  NOT NULL,
    "dec"            double precision  NOT NULL,

    -- Spectroscopic classification, e.g. 'SN Ia'. NULL until classified.
    "type"           text,
    redshift         double precision,
    discoverydate    timestamptz,
    discoverymag     double precision,

    -- Comma-separated survey cross-identifiers as TNS publishes them.
    internal_names   text,
    reporting_group  text,

    -- Provenance: the UTC date of the TNS source file that last wrote this row.
    -- Set by both the daily full snapshot and the hourly delta, so a row first
    -- seen in a delta still has a value.
    source_snapshot  date              NOT NULL,
    refreshed_at     timestamptz       NOT NULL
);

COMMENT ON TABLE {{schema}}.{{table}} IS
    'Mirror of the TNS public objects catalogue. Written only by tns-mirror-server.';
COMMENT ON COLUMN {{schema}}.{{table}}.source_snapshot IS
    'UTC date of the TNS file (daily snapshot or hourly delta) that last wrote this row.';

-- Cone-search prefilter. The server performs no cone search itself; this index
-- exists because it is part of the contract consumers query through.
CREATE INDEX IF NOT EXISTS {{table}}_ra_dec_idx ON {{schema}}.{{table}} (ra, "dec");
CREATE INDEX IF NOT EXISTS {{table}}_name_idx   ON {{schema}}.{{table}} (name);

-- ---------------------------------------------------------------------------
-- Mirror metadata. Single row. Readable by consumers so a client can assert the
-- schema major it was built against and judge freshness before trusting a query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {{schema}}.tns_mirror_meta (
    id                    smallint     PRIMARY KEY,
    schema_version        integer      NOT NULL,
    last_full_sync_at     timestamptz,
    last_delta_sync_at    timestamptz,
    last_source_snapshot  date,
    CONSTRAINT tns_mirror_meta_singleton CHECK (id = 1)
);

INSERT INTO {{schema}}.tns_mirror_meta (id, schema_version)
VALUES (1, 1)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE {{schema}}.tns_mirror_meta IS
    'tns-mirror contract version and sync freshness. Single row (id = 1).';
