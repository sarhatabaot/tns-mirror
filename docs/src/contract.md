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

Any language with a Postgres driver is a first-class consumer. `tns-mirror-client`
(Python) will be a convenience over this, not a privileged path.

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

## `tns_mirror_meta` — version and freshness

Exactly one row, `id = 1`.

| Column | Type | Notes |
|---|---|---|
| `schema_version` | `integer` | The contract major. Assert it matches what you built against |
| `last_full_sync_at` | `timestamptz` | Last committed full snapshot; NULL if none |
| `last_delta_sync_at` | `timestamptz` | Last committed delta that wrote rows |
| `last_source_snapshot` | `date` | UTC date of the most recent full snapshot file |

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

## Access

Consumers connect with a least-privilege role that can `SELECT` on those two
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
