---
layout: layouts/base.liquid
title: Configuration
summary: One YAML file, every value overridable by environment variable. The environment always wins.
permalink: /configuration/
section: server
order: 2
tags: docs
---

Settings resolve in three layers, lowest priority first:

```
built-in defaults  <  config file  <  environment
```

The config file is optional — the defaults below are what runs when you supply
nothing but a credential. Point at a file with `--config` or `$TNS_MIRROR_CONFIG`.

Values in the file expand `${VAR}` and `${VAR:-default}` from the environment, so
secrets stay out of the file and the file itself is safe to commit.

## Authentication

Exactly one mode is active. With neither configured the download refuses; there
is no anonymous fallback.

| Setting | Environment | Default |
|---|---|---|
| `auth.mode` | `TNS_AUTH_MODE` | `marker` |
| `auth.user_agent` | `TNS_USER_AGENT` | — required in `marker` mode |
| `auth.api_key` | `TNS_API_KEY` | — required in `bot` mode |
| `auth.bot_id` | `TNS_BOT_ID` | — required in `bot` mode |
| `auth.bot_name` | `TNS_BOT_NAME` | — required in `bot` mode |

`marker` sends your registered User-Agent on a `GET`. `bot` submits the `api_key`
as form data on a `POST`, alongside a marker naming the bot.

## Download

| Setting | Environment | Default |
|---|---|---|
| `download.url` | `TNS_URL` | the TNS public-objects zip |
| `download.timeout_seconds` | `TNS_TIMEOUT` | `120` |
| `download.throttle_seconds` | `TNS_THROTTLE` | `10` |
| `download.workdir` | `TNS_WORKDIR` | `/var/lib/tns-mirror` |

`throttle_seconds` is the pause between consecutive downloads during a catch-up.
Do not lower it without a reason: TNS rate-limits, and a `429` stops the catch-up
early.

`workdir` holds the transient zip and CSV. It is safe to wipe — losing it costs
one re-download.

The URL must be `https` and must contain `tns_public_objects.csv.zip`, because
the hourly delta filename is derived by rewriting that name. Both are checked at
startup rather than discovered at 00:10.

## Database

The server holds **write** credentials to its own database. These are never
shared with consumers.

| Setting | Environment | Default |
|---|---|---|
| `database.dsn` | `DATABASE_URL` | — (falls back to the standard `PG*` variables) |
| `database.schema` | `TNS_SCHEMA` | `public` |
| `database.table` | `TNS_TABLE` | `tns_objects` |

`schema` and `table` must be plain lowercase identifiers. They reach DDL, so they
are validated at load and composed through psycopg's identifier quoting at use.

## Schedule

Used only by `serve`. Kubernetes deployments run CronJobs against the one-shot
commands and ignore this section entirely. All times are **UTC**, because TNS
stages its hourly files on UT hours and a local-time schedule would drift off
them twice a year.

| Setting | Environment | Default | What it runs |
|---|---|---|---|
| `schedule.full_cron` | `TNS_FULL_CRON` | `0 0 * * *` | the daily full snapshot |
| `schedule.delta_cron` | `TNS_DELTA_CRON` | `10 * * * *` | the hourly delta |
| `schedule.catchup_cron` | `TNS_CATCHUP_CRON` | `30 0 * * *` | a daily 24-hour catch-up |
| `schedule.catchup_hours` | `TNS_CATCHUP_HOURS` | `1` | window for the hourly job |
| `schedule.catchup_window_hours` | `TNS_CATCHUP_WINDOW_HOURS` | `24` | window for the daily catch-up |

An empty cron string disables that job.

The daily catch-up exists to repair hours missed while the server was down.
Deltas are idempotent, so re-applying an hour that already landed is a no-op.

Expressions support the usual five fields with `*`, numbers, lists (`1,15`),
ranges (`9-17`) and steps (`*/15`). Day-of-month and day-of-week are OR-ed when
both are restricted, as in standard cron.

## A complete example

```yaml
source: tns

auth:
  mode: ${TNS_AUTH_MODE:-marker}
  user_agent: ${TNS_USER_AGENT}

download:
  url: https://www.wis-tns.org/system/files/tns_public_objects/tns_public_objects.csv.zip
  timeout_seconds: 120
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
  catchup_hours: 1
  catchup_window_hours: 24
```

## Secrets

Credentials — the marker, the bot `api_key`, and the database DSN — are
operator-supplied and never baked into the image or committed. The configuration
objects redact them from their string representation, so a stray log line or a
traceback cannot leak one.
