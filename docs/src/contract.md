---
layout: layouts/base.liquid
title: The data contract
summary: The schema is the interface. It is the only thing the server and its consumers share.
permalink: /contract/
section: client
order: 4
tags: docs
---

`tns-mirror-server` writes the database; everything else reads it. There is no
shared code between the server and its consumers — after the server is extracted
to its own repository there *cannot* be — so the table definition is the entire,
durable contract.

Any language with a Postgres driver is a first-class consumer.
[`tns-mirror-client`]({{ '/client/' | url }}) (Python) is a convenience over
this, not a privileged path, and the optional
[HTTP API]({{ '/api/' | url }}) serves that same client to anyone who cannot
reach Postgres at all.

The current contract is **v{{ site.schemaVersion }}**, published as
[`schema_v{{ site.schemaVersion }}.sql`]({{ site.repository }}/blob/main/schema/schema_v{{ site.schemaVersion }}.sql).

## `tns_objects` — the catalogue

| Column | Type | Null | Notes |
|---|---|---|---|
| `objid` | `bigint` | no | **Primary key.** The stable TNS identifier, and the key hourly deltas upsert on |
| `name` | `text` | no | IAU name, prefix and name joined: `AT2026abc`, `SN2026xyz` |
| `ra` | `double precision` | no | J2000, degrees, `[0, 360)` |
| `dec` | `double precision` | no | J2000, degrees, `[-90, 90]` |
| `type` | `text` | yes | Spectroscopic classification, e.g. `SN Ia`. NULL until classified |
| `redshift` | `double precision` | yes | |
| `discoverydate` | `timestamptz` | yes | TNS publishes naive UTC; stored as UTC |
| `discoverymag` | `double precision` | yes | |
| `internal_names` | `text` | yes | Comma-separated survey cross-ids, as TNS publishes them |
| `reporting_group` | `text` | yes | |
| `source_snapshot` | `date` | no | UTC date of the TNS file that last wrote this row |
| `refreshed_at` | `timestamptz` | no | When this row was last written |

Indexes: `(ra, dec)` and `(name)`.

Two things worth knowing before you write a query:

- **`name` is not unique-constrained.** TNS names are unique in practice, but a
  delta may carry a rename that transiently collides with a not-yet-updated row.
  Aborting a legitimate delta would be worse than a momentary duplicate, so order
  deterministically — `ORDER BY name, objid` — if you look up by name.
- **Everything except the six `NOT NULL` columns can be null**, and a minimal TNS
  row is common. Do not assume `type` or `redshift` is populated.

## `tns_historic_sne` — the pre-2016 supernovae

**Added in v1.1.** TNS took over IAU supernova naming from the CBAT on
2016-01-01 and imported the earlier SNe — SN 1885A through SN 2015bh, about
6,600 objects — as a closed list. They are **not** in `tns_objects`, because
TNS's staged catalogue file excludes them; they are exposed only through the TNS
search page (`isTNS_AT=no`), whose export has a different shape. So they have
their own table, and TNS's columns are kept **as published**: text, uninterpreted.

| Column | Type | Null | Notes |
|---|---|---|---|
| `objid` | `bigint` | no | **Primary key.** TNS's `ID`. Same identifier space as `tns_objects.objid`; the two never collide |
| `name` | `text` | no | TNS's `Name` with whitespace removed: `SN2015bh`. The one normalised column, so a name lookup works across both tables |
| `ra` | `double precision` | no | J2000 degrees, **derived** from `ra_hms`, so the cone-search index exists here too |
| `dec` | `double precision` | no | J2000 degrees, **derived** from `dec_dms` |
| `ra_hms` | `text` | no | TNS's `RA` verbatim: `09:09:35.059` |
| `dec_dms` | `text` | no | TNS's `DEC` verbatim: `+33:07:22.12` |
| `obj_type` | `text` | yes | `Obj. Type`, e.g. `SN IIn` |
| `redshift` | `text` | yes | As exported — a string, often empty |
| `host_name` | `text` | yes | |
| `host_redshift` | `text` | yes | |
| `reporting_groups` | `text` | yes | `Reporting Group/s` — empty for historic rows |
| `discovery_data_sources` | `text` | yes | `Discovery Data Source/s` |
| `classifying_groups` | `text` | yes | `Classifying Group/s` |
| `associated_groups` | `text` | yes | `Associated Group/s` |
| `disc_internal_name` | `text` | yes | `Disc. Internal Name` |
| `disc_instruments` | `text` | yes | `Disc. Instrument/s` |
| `class_instruments` | `text` | yes | `Class. Instrument/s` |
| `tns_at` | `text` | yes | `TNS AT` — `0` for every historic row |
| `public` | `text` | yes | |
| `end_prop_period` | `text` | yes | `End Prop. Period` |
| `discovery_mag_flux` | `text` | yes | `Discovery Mag/Flux`, e.g. `19.1` |
| `discovery_filter` | `text` | yes | |
| `discovery_date_ut` | `text` | yes | `Discovery Date (UT)`, e.g. `2015-02-07 00:00:00.000` |
| `sender` | `text` | yes | |
| `remarks` | `text` | yes | Discoverer and CBET references live here: `[Discoverer=Itagaki; DiscoveryRef=CBET 4219; ...]` |
| `unreal` | `text` | yes | |
| `discovery_bibcode` | `text` | yes | |
| `classification_bibcodes` | `text` | yes | |
| `ext_catalogs` | `text` | yes | `Ext. catalog/s` |
| `auto_classification` | `text` | yes | |
| `source_snapshot` | `date` | no | UTC date of the historic sync that last wrote this row |
| `refreshed_at` | `timestamptz` | no | When this row was last written |

Indexes: `(ra, dec)` and `(name)`, matching `tns_objects`.

Empty cells are `NULL`. Numeric-looking columns are deliberately text — cast at
query time (`redshift::double precision`) if you need a number; the mirror does
not decide for you which of TNS's strings are numbers. The whole table is
replaced on each historic sync (weekly by default) in one transaction, exactly
like the daily snapshot, and independently of it: a full refresh of
`tns_objects` never touches this table, and vice versa.

## `tns_mirror_meta` — version and freshness

Exactly one row, `id = 1`.

| Column | Type | Notes |
|---|---|---|
| `schema_version` | `integer` | The contract major. Assert it matches what you built against |
| `last_full_sync_at` | `timestamptz` | Last committed full snapshot; NULL if none |
| `last_delta_sync_at` | `timestamptz` | Last committed delta that wrote rows |
| `last_source_snapshot` | `date` | UTC date of the most recent full snapshot file |
| `last_historic_sync_at` | `timestamptz` | Last committed historic-SNe sync; NULL if none. *Added in v1.1* |

Freshness lives here rather than being inferred from a row count, because a row
count looks identical whether the mirror synced an hour ago or died three weeks
ago:

```sql
SELECT schema_version,
       now() - greatest(last_full_sync_at, last_delta_sync_at) AS staleness
FROM tns_mirror_meta;
```

These timestamps are written **in the same transaction as the data**, so they can
never claim a sync that did not commit.

## Guarantees

- **The full snapshot is atomic.** The daily refresh is a delete-then-insert
  inside one transaction. A reader sees either the previous catalogue or the new
  one, never a partial one, and de-published objects disappear on the swap.
- **Deltas are idempotent.** They upsert on `objid`; replaying an hour is a no-op.
- **A failed sync changes nothing.** The last good snapshot stays intact and
  fully queryable.
- **The two catalogue tables are independent.** A full snapshot of `tns_objects`
  never deletes from `tns_historic_sne`, and a historic sync never deletes from
  `tns_objects`. Each is swapped whole, in its own transaction.

## Access

Consumers connect with a least-privilege role that can `SELECT` on those three
tables and nothing else:

```console
$ tns-mirror-server print-grants --database tnsdb --role tns_ro
```

The generated SQL deliberately omits `ALTER DEFAULT PRIVILEGES`, which would hand
the reader `SELECT` on every table created in the schema afterwards. Write
credentials belong to the server alone.

## Cone search

The server does not do this for you — querying the catalogue is the consumer's
job, and [`tns-mirror-client`](/client/) does it correctly for Python. The
portable approach needs no database extension: a bounding-box prefilter that uses
the `(ra, dec)` index, then an exact great-circle test.

```sql
SELECT objid, name, type, redshift,
       2 * degrees(asin(least(1, sqrt(
           power(sin((radians("dec") - radians(:dec)) / 2), 2)
         + cos(radians(:dec)) * cos(radians("dec"))
         * power(sin((radians(ra) - radians(:ra)) / 2), 2)
       )))) AS separation_deg
FROM tns_objects
WHERE ra BETWEEN :ra_min AND :ra_max
  AND "dec" BETWEEN :dec_min AND :dec_max
ORDER BY separation_deg, objid
LIMIT 1;
```

Four things to get right, each of which fails *silently* — returning fewer
matches rather than an error:

1. **Use haversine, not the law of cosines.** `acos` of a dot product is
   ill-conditioned as the separation approaches zero, which is precisely where a
   cross-match works: an object matched against itself comes back at roughly 3
   milliarcseconds instead of 0. The form above stays accurate all the way down.
   Clamp before `asin` regardless — rounding can push its argument past 1 for
   near-antipodal rows.
2. **Compute the RA half-width exactly**, as `asin(sin(radius) / cos(dec))`. The
   familiar `radius / cos(dec)` under-covers as the cone widens, and a prefilter
   that under-covers drops real matches.
3. **Drop the RA bound when the cone reaches a pole** — when
   `sin(radius) >= cos(dec)`. Two objects 177° apart in RA can be 0.2° apart on
   the sky.
4. **OR two ranges at the 0/360 seam.** `ra BETWEEN 359.5 AND 0.5` matches
   nothing.

Pad the box by a hair, too. It is only a prefilter, so being slightly generous
costs nothing, while being slightly tight loses an object sitting exactly on the
radius, where floating-point rounding decides membership by coin flip.

## Versioning

The contract and the client move in lockstep by major: **client 1.x speaks schema
v1**, and a consumer pins `tns-mirror-client>=1,<2`.

| Change | Version | Breaks readers? |
|---|---|---|
| Rename or drop a column, change a type or a unit | **major** | yes — announced in the changelog |
| Add a nullable column, add an index | **minor** | no |

Migrations are forward-only. A released migration is never edited: the server
records a checksum when it applies one and refuses to start if the file changed
afterwards, and CI rejects the edit at review time. A contract change is a
deliberate, versioned, announced event — never a silent edit.
