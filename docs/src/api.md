---
layout: layouts/base.liquid
title: HTTP API
summary: An optional read-only HTTP service in front of a mirror, for consumers who cannot reach Postgres.
permalink: /api/
section: client
order: 3
tags: docs
---

Optional. If your consumers speak Python and can reach the database, the
[client]({{ '/client/' | url }}) is simpler and one hop shorter. This exists for
everything else: a browser, another language, a network where only HTTP crosses
the boundary.

```console
$ curl 'https://your-host/v1/nearest?ra=203.1&dec=10.2&radius_arcsec=3'
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

## It is a transport, not a second implementation

Every query goes through `tns-mirror-client`. The cone search has edge cases
that fail *silently* — returning fewer matches rather than an error — so the
geometry lives in exactly one place and this serves it over HTTP. A test asserts
that a search across the 0/360 seam works through the API, and it passes purely
because the client handles it.

## Why it is a separate image

The sync server holds **write** credentials and is never exposed. This service
holds the **read-only** role, so an exploit here reaches something that can
`SELECT` two tables and nothing else. It also sets its database session
read-only, so a bug in the service cannot write even if it were handed a
privileged role by mistake.

That separation is the reason it is a second container rather than a flag on the
first.

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
| `GET /docs` | OpenAPI, browsable |

**Point a load balancer at `/readyz`, not `/healthz`.** Liveness answers "should
this container be restarted", and restarting does not fix a stale mirror —
probing readiness for that decision would put the service in a restart loop
while it waits for a sync it cannot perform itself.

## Running it

Alongside the mirror, with the reader you created from
[`print-grants`]({{ '/quickstart/' | url }}):

```console
$ docker compose -f docker-compose.yml -f docker-compose.api.yml up -d
```

Set `TNS_RO_PASSWORD` to that reader's password. The compose file binds to
`127.0.0.1` by default — put your own ingress in front rather than exposing it
directly.

Browse `http://127.0.0.1:8000/docs` for the generated OpenAPI.

## Rate limiting

Per caller, per minute, generous by default. Exceeding it returns `429`, and
every response carries `RateLimit-Limit`, `RateLimit-Remaining` and
`RateLimit-Reset`.

<div class="callout" data-callout="warning">

**`TNS_API_TRUST_PROXY` defaults to off, and that default is the
security-relevant part.** Behind a proxy every caller shares the proxy's address,
so the limit applies to the proxy rather than to anyone using it — turning this
on fixes that by reading `X-Forwarded-For`. With it on and *no* proxy in front,
any caller can set that header themselves and mint a fresh bucket per request.
Turn it on only when something you control sets it.

</div>

The limiter is in-process, so each replica has its own budget. For one shared
limit across replicas, put it in front of them.

## Behind a reverse proxy

`TNS_API_ROOT_PATH=/tns` moves every route under the prefix, and the OpenAPI
spec advertises it so the "try it" button in `/docs` points somewhere real. The
container healthcheck follows it too. `tns`, `/tns`, `tns/` and `/tns/` all mean
the same thing.

```nginx
location /tns/ {
    proxy_pass http://tns-api:8000/tns/;
    # Overwrite, not $proxy_add_x_forwarded_for. The limiter charges the
    # LEFT-MOST entry, and appending leaves that slot caller-controlled.
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Use `$remote_addr` when this nginx is the edge, and
`$proxy_add_x_forwarded_for` only when another proxy you trust sits in front of
it — appending puts nginx's view to the *right* of whatever the client sent, so
at the edge a caller keeps the left-most slot and mints a fresh bucket per
request.

Leave it empty if your proxy *strips* the prefix before forwarding — the service
is then already being asked for the paths it serves.

### Keyed access, in tiers

To authenticate callers and give some of them more headroom than others, put a
small nginx gateway inside your own stack: it maps an `X-API-Key` to a consumer
name, the name selects a tier, and each tier is a separate API container with
its own pool and ceilings. Rate limiting then keys on the API key rather than on
an address, which removes the need to trust `X-Forwarded-For` at all.

The API itself stays unchanged and holds no keys — see
[`quickstart/docker-compose.api-gateway.yml`]({{ site.repository }}/blob/main/quickstart/docker-compose.api-gateway.yml)
and the [quickstart README]({{ site.repository }}/blob/main/quickstart/README.md).

## Query caps

`radius_arcsec` and `limit` are **clamped, not rejected**. A caller asking for
too much gets the largest answer the service will serve rather than an error to
handle, and `/v1/cone` echoes back the radius actually used. Rate limiting alone
would not stop a single enormous query, which is what the caps are for.

Defaults are one degree and 1000 results; both are configurable.

## Configuration

Full reference in the
[package README]({{ site.repository }}/blob/main/api/README.md). The ones that
matter most:

| Variable | Default | |
|---|---|---|
| `PGHOST` `PGUSER` `PGPASSWORD` `PGDATABASE` | — | the **read-only** role |
| `TNS_API_RATE_LIMIT` | `60` | requests per minute, per caller |
| `TNS_API_TRUST_PROXY` | `false` | see above |
| `TNS_API_ROOT_PATH` | — | mount under a prefix |
| `TNS_API_MAX_AGE_HOURS` | `26` | staleness threshold for `/readyz` |
| `TNS_API_CORS_ORIGINS` | — | unset means no CORS headers |
