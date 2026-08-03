# tns-mirror-api

Optional read-only HTTP access to a **tns-mirror** catalogue — a local mirror of
the [IAU Transient Name Server](https://www.wis-tns.org) public objects
catalogue.

This image does not download anything and does not own a database. It serves an
existing mirror over HTTP, for consumers that cannot reach Postgres directly: a
browser, another language, a network where only HTTP crosses the boundary.

> **This is not an official TNS service.** It is an independently-operated mirror
> *of* the TNS public catalogue. TNS's data-use terms and citation requirements
> apply to the data.

- **Documentation:** https://sarhatabaot.github.io/tns-mirror/api/
- **Source:** https://github.com/sarhatabaot/tns-mirror
- **The mirror itself:** [`sarhatabaot/tns-mirror-server`](https://hub.docker.com/r/sarhatabaot/tns-mirror-server)
- **Python client:** [`tns-mirror-client`](https://pypi.org/project/tns-mirror-client/) on PyPI

---

## You need a mirror first

Run [`tns-mirror-server`](https://hub.docker.com/r/sarhatabaot/tns-mirror-server)
and create a read-only role:

```console
$ docker compose exec server tns-mirror-server print-grants --database tnsdb
```

Give **that** role's credentials to this image — never the writer's. That
separation is the reason this is a second container: the sync server holds write
credentials and is not exposed, so an exploit here reaches a role that can
`SELECT` two tables and nothing else. The service also sets its database session
read-only, so a bug in it cannot write even through an over-privileged role.

## Quick start

```yaml
services:
  api:
    image: sarhatabaot/tns-mirror-api:1.0.2
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      PGHOST: db
      PGUSER: tns_ro
      PGPASSWORD: the-reader-password
      PGDATABASE: tnsdb
    restart: unless-stopped
    read_only: true
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp
```

```console
$ curl 'http://127.0.0.1:8000/v1/nearest?ra=203.1&dec=10.2&radius_arcsec=3'
```

```json
{
  "objid": 2,
  "name": "SN2026xyz",
  "ra": 203.1005,
  "dec": 10.2,
  "type": "SN Ia",
  "redshift": 0.031,
  "internal_names": ["ZTF26aaa", "ATLAS26x"],
  "separation_arcsec": 0.354
}
```

Browsable OpenAPI at `/docs`.

## Endpoints

| | |
|---|---|
| `GET /v1/nearest?ra=&dec=&radius_arcsec=` | closest object, or `404` |
| `GET /v1/cone?ra=&dec=&radius_arcsec=&limit=` | everything in range, nearest first |
| `GET /v1/objects/{name}` | look up by IAU name |
| `GET /v1/objid/{objid}` | look up by TNS objid |
| `GET /v1/meta` | schema version, row count, freshness |
| `GET /healthz` | liveness; does not touch the database |
| `GET /readyz` | readiness; `503` if the database is unreachable *or the mirror is stale* |
| `GET /docs` | OpenAPI |

**Point a load balancer at `/readyz`, not `/healthz`.** Liveness answers "should
this container be restarted", and restarting does not fix a stale mirror —
probing readiness for that decision would put the container in a restart loop
while it waits for a sync it cannot perform itself. The image's own `HEALTHCHECK`
uses `/healthz` for the same reason.

## Configuration

| Variable | Default | |
|---|---|---|
| `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE` | — | the **read-only** role |
| `DATABASE_URL` | — | alternative to the above; percent-encode `/` and `@` in the password |
| `TNS_SCHEMA`, `TNS_TABLE` | `public`, `tns_objects` | if the mirror was deployed elsewhere |
| `TNS_API_RATE_LIMIT` | `60` | requests per minute, per caller |
| `TNS_API_TRUST_PROXY` | `false` | honour `X-Forwarded-For` — see below |
| `TNS_API_ROOT_PATH` | — | mount every route under a prefix, e.g. `/tns` |
| `TNS_API_MAX_RADIUS_ARCSEC` | `3600` | larger requests are clamped, not rejected |
| `TNS_API_MAX_LIMIT` | `1000` | |
| `TNS_API_MAX_AGE_HOURS` | `26` | staleness threshold for `/readyz` |
| `TNS_API_POOL_MIN`, `TNS_API_POOL_MAX` | `1`, `10` | connection pool |
| `TNS_API_CORS_ORIGINS` | — | comma-separated; unset means no CORS headers |
| `TNS_API_HOST`, `TNS_API_PORT` | `0.0.0.0`, `8000` | |
| `TNS_API_WORKERS` | `1` | granian workers |
| `TNS_API_LOG_LEVEL` | `info` | granian log level |

### Rate limiting

Per caller, per minute, generous by default — this is a read API over a catalogue
that is already public. Exceeding it returns `429`, and every response carries
`RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`.

**`TNS_API_TRUST_PROXY` defaults to off, and that default is the
security-relevant part.** Behind a proxy every caller shares the proxy's address,
so the limit would apply to the proxy rather than to anyone using it; turning
this on fixes that by reading `X-Forwarded-For`. With it on and *no* proxy in
front, any caller can set that header themselves and mint a fresh bucket per
request. Turn it on only when something you control sets it.

The limiter is in-process, so each replica has its own budget. For one shared
limit across replicas, put it in front of them.

### Behind a reverse proxy

`TNS_API_ROOT_PATH=/tns` moves every route under the prefix, and the OpenAPI
spec advertises it so the "try it" button in `/docs` points somewhere real. The
container healthcheck follows it too.

```nginx
location /tns/ {
    proxy_pass http://tns-api:8000/tns/;
    # $remote_addr, not $proxy_add_x_forwarded_for, when this nginx is the
    # edge: the limiter charges the left-most entry, and appending leaves
    # that slot caller-controlled.
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Leave it empty if your proxy *strips* the prefix before forwarding.

### Query caps

`radius_arcsec` and `limit` are **clamped, not rejected** — a caller asking for
too much gets the largest answer the service will serve rather than an error to
handle, and `/v1/cone` echoes back the radius actually used. Rate limiting alone
would not stop a single enormous query.

## What this image does *not* do

It does not download from TNS, does not write, and holds no TNS credential —
that is the sync server's job. It also does not reimplement the cone search:
every query goes through `tns-mirror-client`, so the geometry that a cross-match
depends on exists in exactly one place rather than once per consumer.

## Image

- **Tags:** `1.0.2`, `1.0`, `latest`
- **Platforms:** `linux/amd64`, `linux/arm64`
- **Base:** multi-stage [Wolfi](https://github.com/wolfi-dev), digest-pinned;
  compilers exist only in the builder stage
- **User:** non-root, uid `10001`
- Runs happily with `read_only: true` and `no-new-privileges`
- Published with build provenance and an SBOM
- Litestar on [granian](https://github.com/emmett-framework/granian)

**The major version is the schema version.** `1.x.y` speaks schema v1, image and
library alike.

## Licence and attribution

The software is [MIT](https://github.com/sarhatabaot/tns-mirror/blob/main/LICENSE).

The catalogue is **not** the mirror's to license. If you use the data, follow the
[TNS data-use terms](https://www.wis-tns.org/) and cite TNS as they require.
