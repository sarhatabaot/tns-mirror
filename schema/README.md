# The tns-mirror data contract

**This directory is the interface.** `tns-mirror-server` writes the database;
everything else reads it. There is no shared code between the server and its
consumers — after the server is extracted to its own repository there *cannot*
be — so the table definition is the entire, durable contract.

[`schema_v1.sql`](schema_v1.sql) is the current contract, generated from the
server's migrations. Any language with a Postgres driver is a first-class
consumer; `tns-mirror-client` (Python) is a convenience over this, not a
privileged path.

## Current version: **v1**

### `tns_objects` — the catalogue

| Column | Type | Null | Notes |
|---|---|---|---|
| `objid` | `bigint` | no | **Primary key.** The stable TNS identifier, and the key hourly deltas upsert on |
| `name` | `text` | no | IAU name, `name_prefix` + `name` joined: `AT2026abc`, `SN2026xyz` |
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

Two properties worth knowing before you write a query:

- **`name` is not unique-constrained.** TNS names are unique in practice, but a
  delta may carry a rename that transiently collides with a not-yet-updated row.
  Aborting a legitimate delta would be worse than a momentary duplicate, so
  order deterministically (`ORDER BY name, objid`) if you look up by name.
- **Every column except the six above is nullable**, and a minimal TNS row is
  common. Do not assume `type` or `redshift` is populated.

### `tns_mirror_meta` — version and freshness

Exactly one row, `id = 1`.

| Column | Type | Notes |
|---|---|---|
| `schema_version` | `integer` | The contract major. Assert this matches what you built against |
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

## Reading it

Consumers connect with a least-privilege role that can `SELECT` on these two
tables and nothing else. Generate the SQL with:

```console
$ tns-mirror-server print-grants --database tnsdb --role tns_ro
```

Write credentials belong to the server alone and are never shared.

## Guarantees

- **The full snapshot is atomic.** The daily refresh is a delete-then-insert in
  one transaction. A reader sees either the previous catalogue or the new one,
  never a partial one, and de-published objects disappear on the swap.
- **Deltas are idempotent.** They upsert on `objid`; replaying an hour is a
  no-op.
- **A failed sync changes nothing.** The last good snapshot stays intact and
  fully queryable.

## Versioning

The contract and `tns-mirror-client` move in lockstep by major: **client 1.x
speaks schema v1**, and a consumer pins `tns-mirror-client>=1,<2`.

| Change | Version | Breaks readers? |
|---|---|---|
| Rename or drop a column, change a type or a unit | **major** | yes — announced in `CHANGELOG.md` |
| Add a nullable column, add an index | **minor** | no |

Migrations are forward-only. A released migration is never edited: the server
records a checksum when it applies one and refuses to start if the file changed
afterwards, and CI rejects the edit at review time. A contract change is a
deliberate, versioned, announced event — never a silent edit.

`schema_v1.sql` is generated; edit
`server/src/tns_mirror_server/schema/` and run
`python server/scripts/render_schema.py`. CI fails if the two drift.
