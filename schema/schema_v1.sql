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

-- >>> 0002_historic_sne
-- tns-mirror schema v1.1 — the historic (pre-2016) supernovae, verbatim.
--
-- Forward-only. Once released, this file is FROZEN. Additive only: a new table
-- and a new nullable metadata column, so no v1 reader is affected.
--
-- TNS took over IAU supernova naming from the CBAT on 2016-01-01 and imported
-- the earlier SNe (SN 1885A … SN 2015bh, about 6,600 objects) as a closed list.
-- They are NOT in the staged tns_public_objects csv; TNS exposes them only
-- through the search page (isTNS_AT=no). Because that export has a different
-- shape from the staged csv, and because it is a historical record rather than
-- a live feed, it is mirrored into its own table with TNS's columns kept AS
-- PUBLISHED — text, uninterpreted — rather than mapped onto tns_objects.
--
-- Two deliberate exceptions to "verbatim", both additive:
--   * name is normalised to the prefix+name form tns_objects uses
--     ('SN 2015bh' -> 'SN2015bh') so a name lookup works across both tables.
--   * ra/dec are ALSO stored in degrees, derived from TNS's sexagesimal
--     strings, so the (ra, dec) cone-search prefilter index exists here too.
--     The original strings are kept alongside in ra_hms/dec_dms.
--
-- Empty cells are stored as NULL.

-- ---------------------------------------------------------------------------
-- The historic supernovae. One row per pre-2016 SN. Fully replaced on each
-- historic sync, in one transaction, like the daily snapshot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tns_historic_sne (
    -- TNS's "ID". Same identifier space as tns_objects.objid (the historic
    -- list occupies 1..~6600), so the two tables never collide on it.
    objid                    bigint            PRIMARY KEY,

    -- TNS's "Name" with internal whitespace removed: 'SN2015bh'.
    name                     text              NOT NULL,

    -- J2000, degrees — derived from ra_hms / dec_dms. ra in [0,360), dec in [-90,90].
    ra                       double precision  NOT NULL,
    "dec"                    double precision  NOT NULL,

    -- TNS's "RA" and "DEC" exactly as exported: '09:09:35.059', '+33:07:22.12'.
    ra_hms                   text              NOT NULL,
    dec_dms                  text              NOT NULL,

    -- Everything below is TNS's export column of the same name, as text.
    obj_type                 text,   -- "Obj. Type", e.g. 'SN IIn'
    redshift                 text,
    host_name                text,
    host_redshift            text,
    reporting_groups         text,   -- "Reporting Group/s"
    discovery_data_sources   text,   -- "Discovery Data Source/s"
    classifying_groups       text,   -- "Classifying Group/s"
    associated_groups        text,   -- "Associated Group/s"
    disc_internal_name       text,   -- "Disc. Internal Name"
    disc_instruments         text,   -- "Disc. Instrument/s"
    class_instruments        text,   -- "Class. Instrument/s"
    tns_at                   text,   -- "TNS AT" ('0' for every historic row)
    public                   text,
    end_prop_period          text,   -- "End Prop. Period"
    discovery_mag_flux       text,   -- "Discovery Mag/Flux"
    discovery_filter         text,
    discovery_date_ut        text,   -- "Discovery Date (UT)", e.g. '2015-02-07 00:00:00.000'
    sender                   text,
    remarks                  text,   -- discoverer and CBET references live here
    unreal                   text,
    discovery_bibcode        text,
    classification_bibcodes  text,
    ext_catalogs             text,   -- "Ext. catalog/s"
    auto_classification      text,

    -- Provenance: the UTC date of the historic sync that last wrote this row.
    source_snapshot          date              NOT NULL,
    refreshed_at             timestamptz       NOT NULL
);

COMMENT ON TABLE public.tns_historic_sne IS
    'Pre-2016 (CBAT-era) supernovae as TNS exports them (search?isTNS_AT=no). '
    'Columns are verbatim text except name (whitespace removed) and ra/dec '
    '(degrees, derived from ra_hms/dec_dms). Written only by tns-mirror-server.';
COMMENT ON COLUMN public.tns_historic_sne.ra IS
    'J2000 degrees, derived from ra_hms. Present so the cone-search prefilter index exists.';
COMMENT ON COLUMN public.tns_historic_sne."dec" IS
    'J2000 degrees, derived from dec_dms.';

-- Cone-search prefilter and name lookup, mirroring tns_objects.
CREATE INDEX IF NOT EXISTS tns_historic_sne_ra_dec_idx ON public.tns_historic_sne (ra, "dec");
CREATE INDEX IF NOT EXISTS tns_historic_sne_name_idx   ON public.tns_historic_sne (name);

-- ---------------------------------------------------------------------------
-- Freshness for the historic table. Nullable, so a v1.0 reader is unaffected.
-- ---------------------------------------------------------------------------
ALTER TABLE public.tns_mirror_meta
    ADD COLUMN IF NOT EXISTS last_historic_sync_at timestamptz;

COMMENT ON COLUMN public.tns_mirror_meta.last_historic_sync_at IS
    'Last committed historic-SNe sync; NULL if none.';
