---
layout: layouts/base.liquid
title: Quickstart
summary: One docker-compose.yml, two lines to edit, and about five minutes.
permalink: /quickstart/
section: server
order: 1
tags: docs
---

You need Docker and a free TNS account. You do **not** need to clone anything —
the compose file below pulls the published image.

## 1. Get a TNS credential

**The mirror never downloads anonymously.** TNS access is per-account, and with
no credential the server stops with a message rather than hammering TNS as an
unknown client. This step is not optional.

Register at [wis-tns.org]({{ site.tns }}), then take **one** of:

**Marker** — the simplest, and what every account has:

```text
tns_marker{"tns_id":"1234","type":"user","name":"your_username"}
```

**Bot** — create one under *Bot Management* in your TNS profile, for an
`api_key`, a bot id and a bot name.

Either works. The marker is sent on a `GET`; the bot path submits its `api_key`
as form data on a `POST`.

## 2. Save this as `docker-compose.yml`

Two lines to change, both marked. Nothing else needs touching.

```yaml
name: tns-mirror

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-tnsdb}
      POSTGRES_USER: ${POSTGRES_USER:-tns_writer}
      # EDIT 1 of 2 — openssl rand -base64 24
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-CHANGE-ME}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-tns_writer} -d ${POSTGRES_DB:-tnsdb}"]
      interval: 5s
      timeout: 5s
      retries: 20
    restart: unless-stopped

  server:
    image: sarhatabaot/tns-mirror-server:${TNS_MIRROR_VERSION:-latest}
    depends_on:
      db:
        condition: service_healthy
    environment:
      # EDIT 2 of 2 — your TNS marker, keeping the single quotes.
      TNS_USER_AGENT: ${TNS_USER_AGENT:-}

      TNS_AUTH_MODE: ${TNS_AUTH_MODE:-marker}
      TNS_API_KEY: ${TNS_API_KEY:-}
      TNS_BOT_ID: ${TNS_BOT_ID:-}
      TNS_BOT_NAME: ${TNS_BOT_NAME:-}

      DATABASE_URL: postgresql://${POSTGRES_USER:-tns_writer}:${POSTGRES_PASSWORD:-CHANGE-ME}@db:5432/${POSTGRES_DB:-tnsdb}
      TNS_THROTTLE: ${TNS_THROTTLE:-10}
      TNS_FULL_CRON: ${TNS_FULL_CRON:-0 0 * * *}
      TNS_DELTA_CRON: ${TNS_DELTA_CRON:-10 * * * *}
      TNS_CATCHUP_CRON: ${TNS_CATCHUP_CRON:-30 0 * * *}
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

The two edits look like this:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-paste-the-openssl-output-here}
TNS_USER_AGENT: 'tns_marker{"tns_id":"1234","type":"user","name":"your_username"}'
```

<div class="callout" data-callout="warning">

**A `tns_marker` or bot `api_key` identifies *your* TNS account.** If you put one
directly in the compose file, do not commit that file to a public repository. To
keep credentials out of it entirely, see [.env](#prefer-a-env-file) below.

</div>

The annotated version of this file, with the optional settings and their
explanations, is in
[`quickstart/`]({{ site.repository }}/tree/main/quickstart).

## 3. Start it

```console
$ docker compose up -d
```

On first start the server waits for a healthy database, creates its schema,
notices the catalogue is empty, and downloads the full snapshot. That is the
whole catalogue, so give it a minute or two.

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
it keeps itself current: a full snapshot daily, a delta hourly, and a 24-hour
catch-up daily to repair anything missed while it was down.

## 5. Create a reader for your application

Your application does **not** get the server's write credentials:

```console
$ docker compose exec server tns-mirror-server print-grants --database tnsdb > grants.sql
$ docker compose exec -T db psql -U tns_writer -d tnsdb \
    -v pw="$(openssl rand -base64 24)" -f - < grants.sql
```

That role can `SELECT` on exactly two tables — the catalogue and its metadata —
and nothing else. Use the password you generated:

```text
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

See [the client]({{ '/client/' | url }}) for the full API, or
[the contract]({{ '/contract/' | url }}) to query from any other language.

## Prefer a `.env` file?

Every value in the compose file uses the `${VAR:-default}` form, so a `.env`
beside it overrides anything without touching the file itself. Useful when the
compose file is in version control and the credentials are not:

```bash
POSTGRES_PASSWORD=paste-the-openssl-output-here
TNS_USER_AGENT=tns_marker{"tns_id":"1234","type":"user","name":"your_username"}
```

Leave the compose file exactly as published, and `docker compose up -d`.

## Prefer a YAML config file?

Worth having when you would rather keep configuration in version control, with
comments, than spread across environment variables. Create
`config/tns-mirror.yaml`:

```yaml
auth:
  mode: ${TNS_AUTH_MODE:-marker}
  user_agent: ${TNS_USER_AGENT}

download:
  throttle_seconds: 10
  workdir: /var/lib/tns-mirror

database:
  dsn: ${DATABASE_URL}
  schema: public
  table: tns_objects

schedule:
  full_cron: "0 0 * * *"
  delta_cron: "10 * * * *"
  catchup_cron: "30 0 * * *"
```

Then add to the `server` service:

```yaml
    environment:
      TNS_MIRROR_CONFIG: /etc/tns-mirror/tns-mirror.yaml
    volumes:
      - ./config/tns-mirror.yaml:/etc/tns-mirror/tns-mirror.yaml:ro
```

`${VAR}` expands from the environment inside the YAML, so secrets stay out of it.
Full reference in [Configuration]({{ '/configuration/' | url }}).

## Everyday commands

```console
$ docker compose exec server tns-mirror-server status
$ docker compose exec server tns-mirror-server sync           # full snapshot now
$ docker compose exec server tns-mirror-server catch-up 24    # repair a 24h gap
$ docker compose logs -f server
$ docker compose down                                          # stop; data kept
$ docker compose down -v                                       # stop and delete data
```

## Troubleshooting

**`auth.mode is 'marker' but auth.user_agent (TNS_USER_AGENT) is empty`**
The credential is still blank. Edit `TNS_USER_AGENT` in the compose file, or set
it in `.env`.

**`rows: 0` and `last_full_sync_at: never`**
The first snapshot has not finished or has failed — check
`docker compose logs server`. A `429` means TNS is rate-limiting you; the server
backs off and retries on schedule, so this usually resolves itself.

**The server runs but the data looks old**
`status --max-age-hours 26` exits non-zero when the last sync is older than the
limit, which is what the container healthcheck uses. A failed sync leaves the
last good snapshot intact, so the mirror stays queryable while you investigate.

**Reaching the database from your machine**
The database port is deliberately not published — the mirror's database is not an
internet-facing service. Add this under `db` if you need it locally:

```yaml
    ports:
      - "127.0.0.1:5432:5432"
```

## Upgrading

```console
$ docker compose pull && docker compose up -d
```

Migrations apply automatically at startup and are forward-only. Pin
`TNS_MIRROR_VERSION` rather than tracking `latest` for anything you depend on.
