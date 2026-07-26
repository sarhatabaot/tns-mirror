-- ---------------------------------------------------------------------------
-- tns-mirror published schema contract, version 1
--
-- GENERATED FILE — do not edit. Produced from server/src/tns_mirror_server/schema/
-- by server/scripts/render_schema.py; CI fails if this file drifts from them.
--
-- This is the whole interface. Consumers read `tns_objects` and `tns_mirror_meta`
-- through a read-only role (see `tns-mirror-server print-grants`) and share no
-- code with the server. Any language with a Postgres driver is a first-class
-- consumer.
--
-- Versioning: a breaking change (rename or drop a column, change a type or unit)
-- bumps the major and is announced in CHANGELOG.md. An additive change (a new
-- nullable column, a new index) bumps the minor and never breaks a reader.
-- ---------------------------------------------------------------------------

-- >>> 0001_initial
-- tns-mirror schema v1 — the published consumer contract.
--
-- Forward-only. Once released, this file is FROZEN: a breaking change (rename or
-- drop a column, change a type or unit) is a new major and a new migration, never
-- an edit here. See schema/README.md for the versioning policy.
--
-- The schema and table names below are substituted by the migration runner from
-- config (TNS_SCHEMA / TNS_TABLE), and validated as plain SQL identifiers first.
-- The published contract in schema/schema_v1.sql is this file at its defaults.

CREATE SCHEMA IF NOT EXISTS public;

-- ---------------------------------------------------------------------------
-- The catalogue. One row per TNS object.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tns_objects (
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

COMMENT ON TABLE public.tns_objects IS
    'Mirror of the TNS public objects catalogue. Written only by tns-mirror-server.';
COMMENT ON COLUMN public.tns_objects.source_snapshot IS
    'UTC date of the TNS file (daily snapshot or hourly delta) that last wrote this row.';

-- Cone-search prefilter. The server performs no cone search itself; this index
-- exists because it is part of the contract consumers query through.
CREATE INDEX IF NOT EXISTS tns_objects_ra_dec_idx ON public.tns_objects (ra, "dec");
CREATE INDEX IF NOT EXISTS tns_objects_name_idx   ON public.tns_objects (name);

-- ---------------------------------------------------------------------------
-- Mirror metadata. Single row. Readable by consumers so a client can assert the
-- schema major it was built against and judge freshness before trusting a query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tns_mirror_meta (
    id                    smallint     PRIMARY KEY,
    schema_version        integer      NOT NULL,
    last_full_sync_at     timestamptz,
    last_delta_sync_at    timestamptz,
    last_source_snapshot  date,
    CONSTRAINT tns_mirror_meta_singleton CHECK (id = 1)
);

INSERT INTO public.tns_mirror_meta (id, schema_version)
VALUES (1, 1)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE public.tns_mirror_meta IS
    'tns-mirror contract version and sync freshness. Single row (id = 1).';
