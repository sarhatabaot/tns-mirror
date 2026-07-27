---
layout: layouts/base.liquid
title: Operations
summary: Commands, the two deployment shapes, and what to monitor.
permalink: /operations/
section: server
order: 3
tags: docs
---

## Commands

One-shot commands are the primitive; the long-running `serve` is a thin wrapper
around them.

| Command | What it does |
|---|---|
| `migrate` | Apply pending schema migrations. Idempotent |
| `sync` | Download and ingest the daily full snapshot |
| `sync --hour HH` | Ingest a single UT hour's delta |
| `catch-up [N]` | Apply a trailing window of `N` hourly deltas (default 24) |
| `serve` | Run continuously on the configured schedule |
| `status [--max-age-hours N]` | Row count and sync freshness |
| `print-grants` | SQL for a read-only consumer role |

`sync` also accepts `--redownload` (force a fresh fetch — the default) and
`--reuse-existing` (ingest an already-downloaded file without fetching, for
debugging).

**Only three of these need a TNS credential.** `sync`, `catch-up` and `serve`
download, so they refuse at startup without one. `migrate`, `status` and
`print-grants` never contact TNS — you can create the schema, check a row count
and hand out a read-only role on a mirror that has no credential configured
yet.

## Two deployment shapes, one image

Build the image once, deploy it either way.

### Compose

`docker compose up` runs `serve`, which applies migrations, seeds an empty
catalogue with a full snapshot, and then follows the internal scheduler. This is
the "I just want a TNS mirror" path.

### Kubernetes

Run the one-shot commands as `CronJob`s and skip the in-process scheduler
entirely:

| Job | Schedule (UTC) | Command |
|---|---|---|
| full snapshot | `0 0 * * *` | `sync` |
| hourly delta | `10 * * * *` | `catch-up 1` |
| gap repair | `30 0 * * *` | `catch-up 24` |

Run `migrate` as an init container or a one-off Job before the first sync.

Schedules are UTC because TNS stages its hourly files on UT hours.

## Catch-up and rate limits

TNS rate-limits. The mirror sleeps `throttle_seconds` between consecutive
downloads and **stops a catch-up early on HTTP 429** rather than hammering it.

Because each hour commits on its own, an interrupted catch-up keeps every hour it
already applied, and the next run simply continues. Re-running a catch-up over
hours that already landed is a no-op — deltas are idempotent.

Catch-up applies its window **oldest to newest**, so an object edited in several
hours of the window ends at its latest state.

## Monitoring

`status` is the operational view, and `--max-age-hours` makes it a healthcheck —
it exits `3` when the last sync is older than the limit:

```console
$ tns-mirror-server status --max-age-hours 26
schema_version:       1
rows:                 168423
last_full_sync_at:    2026-07-26 00:04:11+00
last_delta_sync_at:   2026-07-26 11:10:38+00
last_source_snapshot: 2026-07-26
```

The image wires this up as its Docker `HEALTHCHECK`. It checks *freshness*, not
liveness, on purpose: a row count is identical whether the mirror synced an hour
ago or died three weeks ago.

Consumers can ask the same question in SQL — see
[`tns_mirror_meta`]({{ '/contract/#tns_mirror_meta--version-and-freshness' | url }}).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | A deliberate failure: bad configuration, missing credential, unreachable database |
| `2` | Usage error |
| `3` | `status` only — the mirror is stale past the given limit |

## When a sync fails

Nothing is lost. A failed or rate-limited sync leaves the last good snapshot
intact and fully queryable — the full refresh only becomes visible when its
single transaction commits. Under `serve`, a failing job is logged and the
scheduler carries on to the next one.

A quiet delta hour, where TNS publishes nothing because nothing changed, is
treated as a no-op rather than a failure. Only the full refresh is strict: a file
that cannot be parsed raises rather than silently replacing a good catalogue with
an empty one.

## Storage

The catalogue is around 10⁵ rows — small. The transient download area
(`TNS_WORKDIR`) holds one zip and one CSV at a time and is safe to wipe; losing
it costs one re-download.
