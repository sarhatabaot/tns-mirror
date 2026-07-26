# tns-mirror-client

Typed, read-only Python access to a [tns-mirror](https://github.com/sarhatabaot/tns-mirror)
database — a local mirror of the [IAU Transient Name Server](https://www.wis-tns.org)
public objects catalogue.

Add the library, get results. No hand-written SQL, and no rate-limited API on
your hot path.

```console
$ pip install 'tns-mirror-client>=1,<2'
```

```python
import os
from tns_mirror_client import TnsMirror

with TnsMirror(dsn=os.environ["TNS_RO_DSN"]) as tns:
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
    if hit:
        print(hit.name, hit.type, hit.redshift, f'{hit.separation_arcsec:.2f}"')

    obj = tns.by_name("AT2026abc")
    print(tns.count(), "objects mirrored")
```

You need a read-only DSN from whoever operates the mirror. This library never
writes, and it carries no TNS credentials — those belong to the server.

## Why not just write the SQL?

Because a cone search on a sphere has three edges that are easy to get wrong and
fail *silently*:

- **The cosine must be clamped** before `acos`, or an exact positional match —
  the most common case in a cross-match — raises a domain error.
- **Near a pole** the RA bound stops meaning anything: two objects 177° apart in
  right ascension can be 0.2° apart on the sky.
- **At the 0/360 seam**, `ra BETWEEN 359.5 AND 0.5` matches nothing at all.

This library handles all three, and keeps the indexed bounding-box prefilter that
makes the query fast. The geometry is tested by sampling the rim of the cone at
every bearing and asserting the prefilter never excludes a point that is
genuinely inside the radius.

## API

| Method | Returns |
|---|---|
| `nearest(ra, dec, radius_arcsec=3.0)` | `TnsObject \| None` — closest match, ties broken on `objid` |
| `search(ra, dec, radius_arcsec, limit=None)` | `list[TnsObject]` — everything in range, nearest first |
| `by_name(name)` | `TnsObject \| None` |
| `by_objid(objid)` | `TnsObject \| None` |
| `by_names(names)` | `dict[str, TnsObject]` — many in one round trip |
| `count()` | `int` |
| `meta()` | `MirrorMeta` — schema version and freshness |

`TnsObject` mirrors the schema columns exactly: `objid`, `name`, `ra`, `dec`,
`type`, `redshift`, `discoverydate`, `discoverymag`, `internal_names`,
`reporting_group`, `source_snapshot`, `refreshed_at` — plus `separation_arcsec`,
populated only by cone queries.

Everything but `objid`, `name`, `ra`, `dec`, `source_snapshot` and `refreshed_at`
is nullable, and a minimal TNS row is common. Do not assume `type` or `redshift`
is populated; `hit.is_classified` says whether TNS has published a
classification.

## Check freshness before you trust it

A row count looks identical whether the mirror synced an hour ago or died three
weeks ago:

```python
meta = tns.meta()
if not meta.is_fresh(max_age_hours=26):
    raise RuntimeError(f"TNS mirror is stale: last sync {meta.age} ago")
```

## Versioning

**Client 1.x speaks schema v1.** The major version tracks the *contract*, not the
project milestone, so pin it:

```
tns-mirror-client>=1,<2
```

You are then insulated from additive schema changes — a new nullable column or a
new index bumps the minor and never breaks a reader. A breaking change bumps both
majors together and is announced in the changelog.

The client reads the mirror's published `schema_version` on connect and raises
`SchemaVersionError` on a mismatch rather than returning quietly wrong answers.
That check needs `SELECT` on `tns_mirror_meta`, which the mirror's
`print-grants` output includes; pass `check_schema_version=False` to skip it.

## Connections

`TnsMirror(dsn=...)` opens lazily and closes with the context manager or
`.close()`. To reuse a connection you already manage — from a pool, say — pass it
instead, and the client will not close what it did not open:

```python
tns = TnsMirror(connection=my_conn)
```

If the mirror was deployed with a non-default `TNS_SCHEMA`/`TNS_TABLE`, pass
`schema=` and `table=`.

The client sets its session read-only as defence in depth, so even a bug in this
library cannot write through a privileged role.

## Dependencies

One: `psycopg`. This gets embedded in other people's applications, so results are
plain dataclasses rather than a validation framework.

Requires Python 3.11+.

## Licence

MIT. The mirrored catalogue itself is not the mirror's to license — follow the
TNS data-use terms and cite TNS as they require.
