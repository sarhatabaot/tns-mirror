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
CREATE TABLE IF NOT EXISTS {{schema}}.tns_historic_sne (
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

COMMENT ON TABLE {{schema}}.tns_historic_sne IS
    'Pre-2016 (CBAT-era) supernovae as TNS exports them (search?isTNS_AT=no). '
    'Columns are verbatim text except name (whitespace removed) and ra/dec '
    '(degrees, derived from ra_hms/dec_dms). Written only by tns-mirror-server.';
COMMENT ON COLUMN {{schema}}.tns_historic_sne.ra IS
    'J2000 degrees, derived from ra_hms. Present so the cone-search prefilter index exists.';
COMMENT ON COLUMN {{schema}}.tns_historic_sne."dec" IS
    'J2000 degrees, derived from dec_dms.';

-- Cone-search prefilter and name lookup, mirroring tns_objects.
CREATE INDEX IF NOT EXISTS tns_historic_sne_ra_dec_idx ON {{schema}}.tns_historic_sne (ra, "dec");
CREATE INDEX IF NOT EXISTS tns_historic_sne_name_idx   ON {{schema}}.tns_historic_sne (name);

-- ---------------------------------------------------------------------------
-- Freshness for the historic table. Nullable, so a v1.0 reader is unaffected.
-- ---------------------------------------------------------------------------
ALTER TABLE {{schema}}.tns_mirror_meta
    ADD COLUMN IF NOT EXISTS last_historic_sync_at timestamptz;

COMMENT ON COLUMN {{schema}}.tns_mirror_meta.last_historic_sync_at IS
    'Last committed historic-SNe sync; NULL if none.';
