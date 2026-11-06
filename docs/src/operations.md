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
| `sync-historic` | Fetch the pre-2016 supernovae from the TNS search page into `tns_historic_sne` |
| `serve` | Run continuously on the configured schedule |
| `status [--max-age-hours N]` | Row count and sync freshness |
| `print-grants` | SQL for a read-only consumer role |

`sync` also accepts `--redownload` (force a fresh fetch — the default) and
`--reuse-existing` (ingest an already-downloaded file without fetching, for
debugging). `sync-historic` accepts `--reuse-existing` for the same reason.

**Only four of these need a TNS credential.** `sync`, `catch-up`, `sync-historic`
and `serve` download, so they refuse at startup without one. `migrate`, `status` and
`print-grants` never contact TNS — you can create the schema, check a row count
and hand out a read-only role on a mirror that has no credential configured
yet.

## Two deployment shapes, one image

Build the image once, deploy it either way.

### Compose

`docker compose up` runs `serve`, which applies migrations, seeds an empty
catalogue with a full snapshot, seeds an empty historic table with the pre-2016
SNe, and then follows the internal scheduler. This is the "I just want a TNS
mirror" path.

The historic seed is best-effort: if TNS rate-limits it on first start the error
is logged, the catalogue still serves, and the weekly job retries.

### Kubernetes

Run the one-shot commands as `CronJob`s and skip the in-process scheduler
entirely:

| Job | Schedule (UTC) | Command |
|---|---|---|
| full snapshot | `0 0 * * *` | `sync` |
| hourly delta | `10 * * * *` | `catch-up 1` |
| gap repair | `30 0 * * *` | `catch-up 24` |
| historic SNe | `0 3 * * 0` | `sync-historic` |

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

## The historic supernovae

TNS's staged files deliberately exclude the supernovae named before it took over
from the CBAT on 2016-01-01 — SN 1885A through SN 2015bh, about 6,600 objects.
TNS's guidance is to fetch them from the search page with `isTNS_AT=no`, and
that is what `sync-historic` does: roughly 66 pages of 100 rows, written into
their own table, [`tns_historic_sne`]({{ '/contract/#tns_historic_sne--the-pre-2016-supernovae' | url }}),
in one transaction, so the catalogue's nightly refresh and the historic list
never touch each other.

TNS "highly discourages" bulk retrieval through the search page. Three things
make this use of it defensible, and `sync-historic` is built around all three:

- **It is small and closed.** Nothing is ever added to the pre-2016 list, so the
  default schedule is weekly, and the fetch is skipped entirely when
  `historic_cron` is empty.
- **It follows TNS's own rate-limit best practice.** Every response's
  `x-rate-limit-remaining` is read; when it reaches zero the next request waits
  `x-rate-limit-reset + 1` seconds instead of provoking a `429`. A `429` that
  arrives anyway is waited out and retried, up to three times.
- **It cannot half-succeed.** Pages stream into a temporary file that lands in
  place only after the last page; an export that comes back as HTML (an expired
  credential) or empty is refused before it reaches the database.

## Monitoring

`status` is the operational view, and `--max-age-hours` makes it a healthcheck —
it exits `3` when the last sync is older than the limit:

```console
$ tns-mirror-server status --max-age-hours 26
schema_version:        1
rows:                  168423
historic_rows:         6568
last_full_sync_at:     2026-07-26 00:04:11+00
last_delta_sync_at:    2026-07-26 11:10:38+00
last_historic_sync_at: 2026-07-20 03:02:47+00
last_source_snapshot:  2026-07-26
```

`--max-age-hours` judges the catalogue's freshness only. The historic table is
refreshed weekly and a stale copy of a closed list is not an outage.

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

## The optional HTTP API

[`tns-mirror-api`]({{ '/api/' | url }}) runs as a separate container against the
same database, using the read-only role. It has its own liveness and readiness
endpoints, and its own rate limiting.

It is not part of the sync server and does not need to be running for the mirror
to stay current — nor does the mirror need it to be healthy.

## Storage

The catalogue is around 10⁵ rows — small. The transient download area
(`TNS_WORKDIR`) holds one zip and one CSV at a time and is safe to wipe; losing
it costs one re-download.
