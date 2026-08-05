# tns-mirror-server

A self-hostable mirror of the [IAU Transient Name Server](https://www.wis-tns.org)
public objects catalogue, in SQL.

TNS publishes its full object catalogue as a daily CSV snapshot plus hourly delta
files. This image downloads them, authenticates properly, and keeps a Postgres
table in sync — so you can cone-search, cross-match and join against TNS locally,
with no rate-limited API on your hot path.

> **This is not an official TNS service.** It is an independently-operated mirror
> *of* the TNS public catalogue. TNS's data-use terms and citation requirements
> apply to the data.

- **Documentation:** https://sarhatabaot.github.io/tns-mirror/
- **Source:** https://github.com/sarhatabaot/tns-mirror
- **Python client:** [`tns-mirror-client`](https://pypi.org/project/tns-mirror-client/) on PyPI

---

## You need a TNS credential

**The mirror never downloads anonymously.** TNS access is per-account, and with no
credential the server stops with a message rather than hammering TNS as an
unknown client. This image ships with no TNS identity — a `tns_marker` or bot
`api_key` identifies a *specific account*, so each deployer supplies their own.

Register free at [wis-tns.org](https://www.wis-tns.org/), then use either:

- **Marker** — your account's `tns_marker` User-Agent, sent on a `GET`. Simplest.
- **Bot** — create one under *Bot Management*; its `api_key` is submitted as form
  data on a `POST`.

## Quick start

```yaml
name: tns-mirror

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: tnsdb
      POSTGRES_USER: tns_writer
      POSTGRES_PASSWORD: CHANGE-ME          # openssl rand -base64 24
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tns_writer -d tnsdb"]
      interval: 5s
      timeout: 5s
      retries: 20
    restart: unless-stopped

  server:
    image: sarhatabaot/tns-mirror-server:1.0.4
    depends_on:
      db:
        condition: service_healthy
    environment:
      # Your TNS marker, keeping the single quotes.
      TNS_USER_AGENT: 'tns_marker{"tns_id":"1234","type":"user","name":"your_username"}'
      # Discrete PG* variables rather than a URL: a generated password often
      # contains '/' or '@', either of which changes how a URL parses.
      PGHOST: db
      PGUSER: tns_writer
      PGPASSWORD: CHANGE-ME
      PGDATABASE: tnsdb
    volumes:
      - workdir:/var/lib/tns-mirror
    restart: unless-stopped
    read_only: true
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp

volumes:
  pgdata:
  workdir:
```

```console
$ docker compose up -d
```

On first start the server waits for a healthy database, creates its schema,
notices the catalogue is empty, and downloads the full snapshot. That is the
whole catalogue, so give it a minute or two. After that it keeps itself current:
a full snapshot daily, a delta hourly, and a 24-hour catch-up daily to repair
anything missed while it was down.

```console
$ docker compose exec server tns-mirror-server status
schema_version:       1
rows:                 168423
last_full_sync_at:    2026-07-27 00:04:11+00
last_delta_sync_at:   2026-07-27 11:10:38+00
last_source_snapshot: 2026-07-27
```

## Commands

`ENTRYPOINT` is `tns-mirror-server`; the default `CMD` is `serve`.

| Command | What it does |
|---|---|
| `serve` | Run continuously on the configured schedule (the default) |
| `migrate` | Apply pending schema migrations. Idempotent |
| `sync` | Download and ingest the daily full snapshot |
| `sync --hour HH` | Ingest a single UT hour's delta |
| `catch-up [N]` | Apply a trailing window of `N` hourly deltas (default 24) |
| `status [--max-age-hours N]` | Row count and sync freshness; exits 3 when stale |
| `print-grants` | SQL for a least-privilege read-only consumer role |

**Two deploy shapes, one image.** Compose runs `serve` with its internal
scheduler. On Kubernetes, run the one-shot commands as `CronJob`s instead —
`sync` daily at `0 0 * * *`, `catch-up 1` hourly at `10 * * * *`, `catch-up 24`
daily at `30 0 * * *` — and skip the in-process scheduler entirely.

Schedules are **UTC**, because TNS stages its hourly files on UT hours.

## Configuration

Everything is an environment variable; a YAML file is optional and the
environment always wins.

### TNS credential — one mode required

| Variable | Default | |
|---|---|---|
| `TNS_AUTH_MODE` | `marker` | `marker` or `bot` |
| `TNS_USER_AGENT` | — | required in `marker` mode |
| `TNS_API_KEY` | — | required in `bot` mode |
| `TNS_BOT_ID` | — | required in `bot` mode |
| `TNS_BOT_NAME` | — | required in `bot` mode |

### Database — write credentials, never shared

| Variable | Default | |
|---|---|---|
| `PGHOST` `PGUSER` `PGPASSWORD` `PGDATABASE` | — | standard libpq variables (recommended) |
| `DATABASE_URL` | — | alternative to the above; percent-encode `/` and `@` in the password |
| `TNS_SCHEMA` | `public` | |
| `TNS_TABLE` | `tns_objects` | |

### Download and schedule

| Variable | Default | |
|---|---|---|
| `TNS_URL` | the TNS public-objects zip | |
| `TNS_TIMEOUT` | `120` | seconds |
| `TNS_THROTTLE` | `10` | seconds between downloads during catch-up |
| `TNS_WORKDIR` | `/var/lib/tns-mirror` | transient download area |
| `TNS_FULL_CRON` | `0 0 * * *` | `serve` only; empty disables |
| `TNS_DELTA_CRON` | `10 * * * *` | `serve` only |
| `TNS_CATCHUP_CRON` | `30 0 * * *` | `serve` only |
| `TNS_CATCHUP_HOURS` | `1` | window for the hourly job |
| `TNS_CATCHUP_WINDOW_HOURS` | `24` | window for the daily catch-up |
| `TNS_MIRROR_CONFIG` | — | path to an optional YAML config |

Do not lower `TNS_THROTTLE` without a reason: TNS rate-limits, and a `429` stops
a catch-up early rather than hammering it.

## Querying the mirror

Your application does **not** get the server's write credentials. Generate a
least-privilege reader:

```console
$ PW=$(openssl rand -base64 24)          # generate it, and keep it
$ echo "$PW"                             # this is the reader's password

$ docker compose exec server tns-mirror-server print-grants --database tnsdb > grants.sql
$ docker compose exec -T db psql -U tns_writer -d tnsdb \
    -v pw="$PW" -f - < grants.sql
```

That role can `SELECT` on exactly two tables — the catalogue and its metadata —
and nothing else.

Then either use the Python client:

```console
$ pip install 'tns-mirror-client>=1,<2'
```

```python
from tns_mirror_client import TnsMirror

with TnsMirror(dsn=TNS_RO_DSN) as tns:
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
    if hit:
        print(hit.name, hit.type, hit.redshift)
```

…or query directly from any language with a Postgres driver. The schema is a
published, versioned contract — see
[the documentation](https://sarhatabaot.github.io/tns-mirror/contract/).

**The major version is the schema version.** `1.x.y` speaks schema v1, image and
library alike, so pinning `tns-mirror-client>=1,<2` pins the contract.

## What this image does *not* do

It downloads, authenticates, and writes. That is all. No cone search, no
cross-matching, no HTTP API — querying the catalogue belongs to the consumer, and
`tns-mirror-client` does it correctly for Python.

## Guarantees

- **The full snapshot is atomic.** The daily refresh is a delete-then-insert in
  one transaction: a reader sees either the previous catalogue or the new one,
  never a partial one, and de-published objects disappear on the swap.
- **Deltas are idempotent.** They upsert on the TNS `objid`; replaying an hour is
  a no-op.
- **A failed sync changes nothing.** The last good snapshot stays intact and fully
  queryable while you investigate.

## Image

- **Tags:** `1.0.4`, `1.0`, `latest`
- **Platforms:** `linux/amd64`, `linux/arm64`
- **Base:** multi-stage [Wolfi](https://github.com/wolfi-dev), digest-pinned;
  compilers exist only in the builder stage
- **User:** non-root, uid `10001`
- **Volume:** `/var/lib/tns-mirror` — transient working area, safe to wipe (costs
  one re-download)
- **Healthcheck:** `status --max-age-hours 26` — checks *freshness*, not
  liveness, because a row count looks identical whether the mirror synced an hour
  ago or died three weeks ago
- Published with build provenance and an SBOM
- Runs happily with `read_only: true` and `no-new-privileges`

Requires a PostgreSQL database you provide. The image holds write credentials to
that database only; consumers get a separate read-only role.

## Licence and attribution

The software is [MIT](https://github.com/sarhatabaot/tns-mirror/blob/main/LICENSE).

The catalogue is **not** the mirror's to license. If you use the data, follow the
[TNS data-use terms](https://www.wis-tns.org/) and cite TNS as they require.
