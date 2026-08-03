# Quickstart

Run a TNS mirror in about five minutes. You need Docker, and a free TNS account.

Take [`docker-compose.yml`](docker-compose.yml), change the two marked lines, and
`docker compose up -d`. It pulls the published image, so there is no source
checkout, no Python, and no build step — you can copy that one file anywhere and
run it. A `.env` is optional, not required.

---

## 1. Get a TNS credential

**The mirror never downloads anonymously.** TNS access is per-account, and with
no credential the server refuses to sync rather than hammering TNS as an unknown
client. So this step is not optional.

Register at [wis-tns.org](https://www.wis-tns.org/), then take **one** of:

**Marker** — the simplest, and what every account has. Your `tns_marker` string
looks like:

```
tns_marker{"tns_id":"1234","type":"user","name":"your_username"}
```

**Bot** — create one under *Bot Management* in your TNS profile. You get an
`api_key`, a bot id, and a bot name.

Either works. The mirror sends the marker on a `GET`; the bot path submits the
`api_key` as form data on a `POST`.

## 2. Edit two lines

Open `docker-compose.yml`. Both edits are marked with a box:

```yaml
# EDIT 1 of 2 — the database password
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-paste-the-openssl-output-here}

# EDIT 2 of 2 — your TNS credential
TNS_USER_AGENT: 'tns_marker{"tns_id":"1234","type":"user","name":"your_username"}'
```

Generate a password with `openssl rand -base64 24`.

> **A `tns_marker` or bot `api_key` identifies *your* TNS account.** If you edit
> the compose file directly, do not commit it to a public repository.

### Or keep credentials out of the file

Every value uses the `${VAR:-default}` form, so a `.env` beside the compose file
overrides anything without touching it. Copy `.env.example` to `.env`, fill in
those same two values, and leave `docker-compose.yml` exactly as published.

## 3. Start it

```console
$ docker compose up -d
```

On first start the server waits for a healthy database, creates its schema,
notices the catalogue is empty, and downloads the full snapshot. That is the
whole catalogue, so give it a minute or two.

```console
$ docker compose logs -f server
```

## 4. Check it worked

```console
$ docker compose exec server tns-mirror-server status
schema_version:       1
rows:                 168423
last_full_sync_at:    2026-07-26 00:04:11+00
last_delta_sync_at:   2026-07-26 11:10:38+00
last_source_snapshot: 2026-07-26
```

A non-zero `rows` and a recent `last_full_sync_at` means you are done. From here
the server keeps itself current: a full snapshot daily, a delta hourly, and a
24-hour catch-up daily to repair anything missed while it was down.

## 5. Create a reader for your application

Your application does **not** get the server's write credentials. Generate a
least-privilege role instead:

```console
$ PW=$(openssl rand -base64 24)          # generate it, and keep it
$ echo "$PW"                             # this is the reader's password

$ docker compose exec server tns-mirror-server print-grants --database tnsdb > grants.sql
$ docker compose exec -T db psql -U tns_writer -d tnsdb \
    -v pw="$PW" -f - < grants.sql
```

`print-grants` writes `:'pw'` where the password goes and `-v pw=…` fills it in,
so the password reaches Postgres without being written to `grants.sql`. That is
the only reason for the indirection — skip it and type the password directly if
you prefer. A reader is just five statements:

```sql
CREATE ROLE tns_ro LOGIN PASSWORD 'your-password';
GRANT CONNECT ON DATABASE tnsdb TO tns_ro;
GRANT USAGE ON SCHEMA public TO tns_ro;
GRANT SELECT ON public.tns_objects TO tns_ro;
GRANT SELECT ON public.tns_mirror_meta TO tns_ro;
```

That role can `SELECT` on exactly two tables — the catalogue and its metadata —
and nothing else. Use the password in your application's DSN:

```
postgresql://tns_ro:THE-PASSWORD@your-host:5432/tnsdb
```

## 6. Query it

```console
$ pip install 'tns-mirror-client>=1,<2'
```

```python
import os
from tns_mirror_client import TnsMirror

with TnsMirror(dsn=os.environ["TNS_RO_DSN"]) as tns:
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
    if hit:
        print(hit.name, hit.type, hit.redshift)
```

Any language with a Postgres driver works too — see
[the data contract](../schema/README.md).

---

## Just the minimum

[`docker-compose.minimal.yml`](docker-compose.minimal.yml) is the same setup with
nothing optional in it, using a **TNS bot**. Replace the four `CHANGE-ME` values
and run:

```console
$ docker compose -f docker-compose.minimal.yml up -d
```

Bot mode needs all three of `TNS_API_KEY`, `TNS_BOT_ID` and `TNS_BOT_NAME` — the
key authenticates the request, the id and name identify which bot is asking.

## Optional: keyed HTTP access, in two tiers

For serving colleagues over HTTP while a couple of high-throughput jobs get
room to run — with every consumer authenticated, and none of it configured
outside your own stack.

```
outer proxy ──► :8080 gateway ──► api        capped tier   60/min, 1000 rows, 1°
                                └─► api-batch  loader tier   unlimited, whole sky
```

Everyone uses **one URL**. The `X-API-Key` header selects the tier, so a
consumer is promoted, demoted or revoked by editing one file and reloading.

```console
$ cp nginx/tns-api-keys.map.example nginx/tns-api-keys.map
$ chmod 600 nginx/tns-api-keys.map
$ openssl rand -hex 20                     # one key per consumer
```

A key's **name** picks its tier — anything starting `batch-` gets the loader
tier, everything else the capped one:

```
"k_7f3a9c21e5b84d06a1f2…"   batch-ingest-alpha;
"k_9e04ab762d3f15c8b0e7…"   team-carol;
```

Then, with a reader role already created (step 5) and `TNS_RO_PASSWORD` set:

```console
$ docker compose -f docker-compose.yml \
                 -f docker-compose.api.yml \
                 -f docker-compose.api-gateway.yml up -d
```

```console
$ curl -H "X-API-Key: k_9e04…" \
    'http://127.0.0.1:8080/tns-mirror/api/v1/cone?ra=203.1&dec=10.2&radius_arcsec=60'
```

### Handing it to whoever runs the reverse proxy

Give them [`nginx/handoff.conf`](nginx/handoff.conf). It is one `location`
block pointing at `127.0.0.1:8080`, and it never needs editing again — not to
add a consumer, rotate a key, or change a limit. No key is ever visible
outside your stack.

### Everyday operations

```console
$ docker compose exec gateway nginx -t              # check before reloading
$ docker compose exec gateway nginx -s reload       # apply a key change
$ docker compose logs -f gateway                    # who called what
```

Reloading is graceful — in-flight requests finish on the old config. The log
records each consumer's **name**, never the key:

```
127.0.0.1 fwd=- key=team-carol [03/Aug/2026:13:04:44 +0300] "GET /tns-mirror/api/v1/objects/SN2026xyz HTTP/1.1" 200 83 0.001s
```

Rate limits are per **key**, not per address, so two projects behind one NAT
get separate budgets and one project across a cluster shares a single budget.
Where each knob lives:

| | |
|---|---|
| who may call, and their tier | `nginx/tns-api-keys.map` — reload |
| requests per minute per tier | `nginx/gateway.conf` — reload |
| query ceilings, pool size | `.env` — recreate |

> **The gateway speaks plain HTTP, so keys cross it in the clear.** That is
> fine over loopback to a proxy on the same host. If anything terminates
> further away, put TLS in front before issuing the first key.

Query ceilings are per **tier**, not per key, because they are per-process
configuration. Both are clamps rather than errors: at the capped tier a
request for 5000 rows returns exactly 1000, with nothing in the response to
say it was truncated.

## Optional: a YAML config file

Useful when you would rather keep your configuration in version control, with
comments, than spread across environment variables.

```console
$ cp config/tns-mirror.yaml.example config/tns-mirror.yaml
```

Then in `docker-compose.yml`, uncomment the two lines marked for it: the
`TNS_MIRROR_CONFIG` environment variable and the config volume mount. Restart:

```console
$ docker compose up -d
```

`${VAR}` expands from the environment inside the YAML, so secrets stay in `.env`
and the config file itself is safe to commit.

## Everyday commands

```console
$ docker compose exec server tns-mirror-server status
$ docker compose exec server tns-mirror-server sync           # full snapshot now
$ docker compose exec server tns-mirror-server catch-up 24    # repair a 24h gap
$ docker compose logs -f server
$ docker compose down                                          # stop; data is kept
$ docker compose down -v                                       # stop and delete data
```

## Troubleshooting

**`auth.mode is 'marker' but auth.user_agent (TNS_USER_AGENT) is empty`**
The credential is still blank. Set `TNS_USER_AGENT` in `docker-compose.yml`, or
in a `.env` beside it.

**`rows: 0` and `last_full_sync_at: never`**
The first snapshot has not finished or has failed. Check
`docker compose logs server`. A `429` means TNS is rate-limiting you — the
server backs off and retries on schedule, so this usually resolves itself.

**The server is running but the data looks old**
`status --max-age-hours 26` exits non-zero when the last sync is older than the
limit; that is what the container's healthcheck uses. Check the logs for the
failing job — a failed sync leaves the last good snapshot intact, so the mirror
stays queryable while you investigate.

**Reaching the database from your machine**
Uncomment the `ports` block under `db` in `docker-compose.yml`. It binds to
`127.0.0.1` deliberately: the mirror's database is not an internet-facing
service.

## Upgrading

```console
$ docker compose pull
$ docker compose up -d
```

Migrations are applied automatically at startup and are forward-only. Pin
`TNS_MIRROR_VERSION` in `.env` rather than tracking `latest` for anything you
depend on.
