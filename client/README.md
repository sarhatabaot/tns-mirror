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

You need a read-only DSN from whoever operates the mirror, or the URL of its HTTP API and a key (see [Over HTTP](#over-http)). This library never
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

## Over HTTP

When you can reach the mirror's [HTTP API](https://sarhatabaot.net/tns-mirror/api/) but not its database, give the client a `url` and an API key instead of a DSN. The methods, the results and the errors are the same:

```console
$ pip install 'tns-mirror-client[http]>=1.1,<2'
```

```python
import os
from tns_mirror_client import TnsMirror

with TnsMirror(
    url="https://example.org/tns-mirror/api/",
    api_key=os.environ["TNS_MIRROR_API_KEY"],
) as tns:
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
```

The answers are identical, not just similar. The API runs this library's SQL path on the server, so the cone, the separation and the tie-break on `objid` are computed in one place, and CI asserts that both transports return equal results for the same seeded mirror, across the 0/360 seam and at both poles.

- **The key** goes in the `X-API-Key` header on every request. It never appears in a URL, a log line, an exception or a `repr()`. A `url` carrying credentials or a query string is refused.
- **Timeouts** are always set: `timeout=5.0` seconds by default for connecting and for reading, or pass a `(connect, read)` pair.
- **One session** is reused for keep-alive, opened on first use and closed by `.close()` or the context manager. Use a client from one thread at a time. To bring your own `requests.Session` (for retries, or a private CA), pass `session=`; the client will not close it.
- **Proxies** follow the environment by default: `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are honoured. To keep the mirror off a proxy, add its host to `NO_PROXY`, or pass `trust_env=False` to ignore all three.
- **HTTPS** verifies certificates. Redirects are not followed, because a redirect could carry the key to another host; point `url` at the final address.
- **The schema check** runs on first use, over `/v1/meta`, exactly as it does over SQL.
- **`schema` and `table`** are the API's configuration over HTTP (`TNS_SCHEMA`, `TNS_TABLE`), not the client's.

The API caps the radius and the number of results it serves in one query. The SQL path has no cap, so rather than return a short answer, the HTTP path raises `QueryLimitExceeded` when the server clamped a query: narrow the radius, or pass a `limit`. A cross-match at a few arcseconds never comes near either cap.

`nearest`, `count` and `meta` work against any version of the API. `search` needs tns-mirror-api 1.1.0 or later to tell a complete answer from a clamped one, and `by_names` needs its `/v1/objects` endpoint, also new in 1.1.0.

### Errors

Every error the client raises deliberately is a `TnsMirrorClientError`, whichever transport you use, so one `except` covers both. No `requests` or `psycopg` exception escapes the HTTP path.

| Error | When |
|---|---|
| `MirrorUnavailable` | unreachable, timed out, TLS failure, HTTP 5xx, rate limited (429), a redirect, or a URL that is not a tns-mirror API |
| `AccessDenied` | HTTP 401 or 403: the key was refused. Retrying with the same key will not help |
| `SchemaVersionError` | the mirror speaks another schema major. Never retry this one |
| `QueryLimitExceeded` | the API would have answered with fewer results than exist |

An argument the API rejects, such as a declination beyond ±90°, raises `ValueError` with the API's reason, as a bad argument does on the SQL path.

## Dependencies

One: `psycopg`. The `http` extra adds `requests`, and only the HTTP path imports it. This gets embedded in other people's applications, so results are
plain dataclasses rather than a validation framework.

Requires Python 3.11+.

## Licence

MIT. The mirrored catalogue itself is not the mirror's to license — follow the
TNS data-use terms and cite TNS as they require.
