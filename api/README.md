# tns-mirror-api

Optional read-only HTTP access to a [tns-mirror](https://github.com/sarhatabaot/tns-mirror)
catalogue. Ships as a Docker image, not on PyPI.

It is a **transport in front of [`tns-mirror-client`](../client)**, not a second
implementation of the cone search. That geometry has three edge cases that fail
*silently* — returning fewer matches rather than an error — so it lives in
exactly one place and this serves it over HTTP.

It connects with the mirror's **read-only role**, which is the reason it is a
separate artifact: the sync server holds write credentials and is not exposed,
and an exploit here reaches a role that can `SELECT` two tables and nothing else.
The session is set read-only as well, so a bug here cannot write even if it were
handed a privileged role by mistake.

## Endpoints

| | |
|---|---|
| `GET /v1/nearest?ra=&dec=&radius_arcsec=` | closest object, or 404 |
| `GET /v1/cone?ra=&dec=&radius_arcsec=&limit=` | everything in range, nearest first |
| `GET /v1/objects/{name}` | look up by IAU name |
| `GET /v1/objid/{objid}` | look up by TNS objid |
| `GET /v1/meta` | schema version, row count, freshness |
| `GET /healthz` | liveness; does not touch the database |
| `GET /readyz` | readiness; 503 if the database is unreachable *or the mirror is stale* |
| `GET /docs` | OpenAPI |

## Configuration

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | — | read-only DSN, or use the standard `PG*` variables |
| `TNS_SCHEMA`, `TNS_TABLE` | `public`, `tns_objects` | if the mirror was deployed elsewhere |
| `TNS_API_RATE_LIMIT` | `60` | requests per minute, per caller |
| `TNS_API_TRUST_PROXY` | `false` | honour `X-Forwarded-For` — see below |
| `TNS_API_MAX_RADIUS_ARCSEC` | `3600` | larger requests are clamped, not rejected |
| `TNS_API_MAX_LIMIT` | `1000` | |
| `TNS_API_MAX_AGE_HOURS` | `26` | staleness threshold for `/readyz` |
| `TNS_API_POOL_MIN`, `TNS_API_POOL_MAX` | `1`, `10` | connection pool |
| `TNS_API_CORS_ORIGINS` | — | comma-separated; unset means no CORS headers |
| `TNS_API_ROOT_PATH` | — | mount every route under a prefix, e.g. `/tns` |
| `TNS_API_HOST`, `TNS_API_PORT` | `0.0.0.0`, `8000` | bind address |
| `TNS_API_WORKERS` | `1` | granian workers |
| `TNS_API_LOG_LEVEL` | `info` | granian log level |

### Rate limiting

Per caller, per minute, generous by default — this is a public read API over a
catalogue that is already public. Exceeding it returns `429`, and every response
carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`.

**`TNS_API_TRUST_PROXY` defaults to off, and that default is the
security-relevant part.** Behind a proxy every caller shares the proxy's address,
so the limit would apply to the proxy rather than to anyone using it; turning
this on fixes that by reading `X-Forwarded-For`. With it on and *no* proxy in
front, any caller can set the header themselves and mint a fresh bucket per
request. Turn it on only when something you control sets that header.

The limiter is in-process, so each replica has its own budget. For one shared
limit across replicas, put it in front of them instead.

### Behind a reverse proxy

Set `TNS_API_ROOT_PATH=/tns` and every route moves under that prefix —
`/tns/v1/nearest`, `/tns/healthz`, `/tns/docs` — and the OpenAPI spec advertises
it, so the "try it" button in `/docs` points somewhere real. The container
healthcheck follows it too. `tns`, `/tns`, `tns/` and `/tns/` all mean the same
thing.

Set it when your proxy **passes the prefix through**:

```nginx
location /tns/ {
    proxy_pass http://tns-api:8000/tns/;
    # $remote_addr, not $proxy_add_x_forwarded_for, when this nginx is the
    # edge: the limiter charges the left-most entry, and appending leaves
    # that slot caller-controlled.
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Leave it **empty** if your proxy strips the prefix before forwarding — the
service is then already being asked for the paths it serves.

Either way, set `TNS_API_TRUST_PROXY=true` so the rate limit applies to callers
rather than to your proxy.

### Query caps

`radius_arcsec` and `limit` are **clamped, not rejected** — a caller asking for
too much gets the largest answer the service will serve rather than an error to
handle. `/v1/cone` echoes back the radius actually used. Rate limiting alone
would not stop a single enormous query.

## Running it

```console
$ docker run --rm -p 8000:8000 \
    -e PGHOST=your-db -e PGUSER=tns_ro -e PGPASSWORD=... -e PGDATABASE=tnsdb \
    sarhatabaot/tns-mirror-api:1.0.4
```

Use the `tns_ro` credentials from `tns-mirror-server print-grants`, never the
writer's.

## Development

```console
$ uv sync --all-groups
$ uv run pytest
$ uv run tns-mirror-api          # granian on :8000
```
