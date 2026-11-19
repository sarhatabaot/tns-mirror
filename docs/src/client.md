---
layout: layouts/base.liquid
title: Python client
summary: Add the library, get typed results. No hand-written SQL, no rate-limited API.
permalink: /client/
section: client
order: 1
tags: docs
---

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
```

You need a read-only DSN from whoever runs the mirror — see
[the quickstart]({{ '/quickstart/' | url }}) — or, if only HTTP reaches you, the URL of its API and a key: see [Over HTTP](#over-http). The library never writes, and
carries no TNS credentials: those belong to the server.

## Why not just write the SQL?

Because a cone search on a sphere has three edges that are easy to get wrong and
that fail *silently* — returning fewer matches rather than an error.

**The formula has to be accurate at small separations.** The obvious choice, the
law of cosines, is ill-conditioned exactly where a cross-match lives: as the
separation approaches zero its argument approaches 1, and `acos` loses most of
its significant digits. An object matched against itself comes back at roughly 3
milliarcseconds instead of zero. This library uses haversine, in both Python and
SQL, and gets 0.0.

**Near a pole the RA bound stops meaning anything.** Two objects 177° apart in
right ascension can be 0.2° apart on the sky. Constraining RA there drops real
matches, so the bound is dropped instead — decided exactly, by comparing
`sin(radius)` with `cos(dec)`, not by an arbitrary declination cut-off.

**At the 0/360 seam a range match finds nothing.** `ra BETWEEN 359.5 AND 0.5` is
empty. The prefilter emits two OR-ed ranges when the cone wraps.

The bounding box is also padded by a hair. It is a prefilter, so being slightly
generous costs nothing — the exact test runs afterwards — while being slightly
tight loses an object sitting exactly on the search radius, where floating-point
rounding decides membership by coin flip.

That property is tested directly: the rim of the cone is sampled at every bearing
from a range of positions, including the seam and near the poles, and the
prefilter must never exclude a sample.

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

`TnsObject` mirrors [the contract]({{ '/contract/' | url }}) exactly, plus
`separation_arcsec`, which is populated only by cone queries. Two conveniences
sit on top: `internal_name_list` splits the survey cross-ids, and
`is_classified` says whether TNS has published a classification.

Everything but `objid`, `name`, `ra`, `dec`, `source_snapshot` and `refreshed_at`
is nullable, and a minimal TNS row is common.

## Check freshness before you trust it

A row count looks identical whether the mirror synced an hour ago or died three
weeks ago:

```python
meta = tns.meta()
if not meta.is_fresh(max_age_hours=26):
    raise RuntimeError(f"TNS mirror is stale — last sync {meta.age} ago")
```

## Versioning

**Client 1.x speaks schema v1.** The major tracks the *contract*, not the project
milestone, so pin it:

```
tns-mirror-client>=1,<2
```

You are then insulated from additive schema changes — a new nullable column or
index bumps the minor and never breaks a reader. A breaking change bumps both
majors together.

On connect the client reads the mirror's published `schema_version` and raises
`SchemaVersionError` on a mismatch, rather than returning quietly wrong answers.
That needs `SELECT` on `tns_mirror_meta`, which the mirror's `print-grants`
output includes. Pass `check_schema_version=False` to skip it.

A contract test in CI reads `schema_v1.sql` and checks the client's model against
it — column names, order, and which columns are nullable. The server and the
client share no code, so that file is the only thing binding them, and drift
between the two is the failure this library most needs to prevent.

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
library cannot write through an over-privileged role.

## Over HTTP

When you can reach the mirror's [HTTP API]({{ '/api/' | url }}) but not its database, give the client a `url` and an API key instead of a DSN. The methods, the results and the errors are the same:

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
plain dataclasses rather than a validation framework. Requires Python 3.11+,
tested on 3.11, 3.12 and 3.13.

## Other languages

There is nothing privileged about the Python client. The
[contract]({{ '/contract/' | url }}) documents the tables, and the worked cone
search there is the same query this library emits — port it and you have a client
in any language with a Postgres driver.

If you cannot reach Postgres at all — a browser, a network where only HTTP
crosses the boundary — the optional [HTTP API]({{ '/api/' | url }}) serves this
same library over HTTP, so you get the same geometry without porting it. From Python, this client speaks it directly: see [Over HTTP](#over-http).
