# tns-mirror — working rules

A TNS→SQL mirror. Two artifacts from one repo: `tns-mirror-server` (Docker image,
owns and writes the database) and `tns-mirror-client` (PyPI, v0.2, reads it).
`schema/` is the contract between them.

## Invariants — do not violate these

1. **The database schema is the only contract.** The server is the sole writer;
   consumers read through a read-only role. There is no shared code between
   server and consumer. Changing the table shape is a versioned contract change.
2. **The mirror knows nothing about its consumers.** No consumer-specific
   fields, names, or logic anywhere in `server/`. Generic TNS→SQL.
3. **Effectively-once, atomic refresh.** The full snapshot is delete-then-insert
   in **one transaction**. Do not split it. Deltas upsert on `objid` and are
   idempotent.
4. **Authenticated to TNS, never anonymous.** A marker or a bot `api_key` must be
   configured; with neither, the download refuses. No anonymous fallback, ever.
5. **Least privilege.** The server holds write credentials to its own database
   only. Consumers get a distinct read-only role. The database is not
   internet-exposed. The container runs non-root.
6. **Self-contained.** Own `pyproject.toml`, lockfile, `.python-version`,
   `Dockerfile`, CI. No reach into any other repo or service.
7. **Fail safe.** A failed or rate-limited sync leaves the last good snapshot
   intact and queryable. A quiet delta hour is a no-op; only the full refresh is
   strict.

## Scope

The server **downloads, authenticates, and writes**. That is all.

Cone search, cross-matching, and deciding what a TNS name *means* belong to the
client or to the consumer — never to the server. If you find yourself adding
geometry to `server/`, stop: it is in the wrong package. The only exception is
the `(ra, dec)` index, which is DDL and therefore part of the contract.

## Architecture

Ports and adapters, not a framework. It is a headless batch job: it needs a
database driver and a scheduler, not Django.

- `ports/` — `Source` and `Store` protocols. The engine depends on these and
  nothing else.
- `adapters/` — the only modules that touch the network, the disk, or SQL. Every
  port has an in-memory fake.
- `engine.py` — provider-agnostic orchestration. **No I/O of its own.**
- `store_postgres.py` — the only module that knows SQL or the table name.

## Changing the schema

1. Add a **new numbered file** in `server/src/tns_mirror_server/schema/`. Never
   edit a released one — the runner checksums applied migrations and refuses to
   start on a mismatch, and CI rejects the edit at review time.
2. Run `python server/scripts/render_schema.py` to regenerate
   `schema/schema_v1.sql`. CI fails if it drifts.
3. Additive (new nullable column, new index) → minor. Breaking (rename, drop,
   type or unit change) → major, in lockstep with the client, announced in
   `CHANGELOG.md`.

## Changing the version

1. Edit `VERSION` at the repository root. That is the only file you edit.
2. Run `python3 scripts/check_versions.py --write` to propagate it into the
   seven derived files and the image tags pinned across the compose files and
   documentation. CI and pre-commit fail if any of them drift.

The derived files hold literals rather than reading `VERSION` at runtime
because the images build with a **narrow context** — `server/` and `api/`, not
the repository root — so nothing inside them can reach a file at the top level.
Each artifact stays self-contained; the script keeps them agreeing.

**The major version is the schema version**, so `SCHEMA_VERSION` is deliberately
not derived from `VERSION`; the check enforces that they match. Tags are bare
(`1.0.4`, not `v1.0.4`).

## Conventions

- Python ≥ 3.13, uv, hatchling, ruff (lint + format).
- Dependencies are attack surface in a published image. The current set is
  `requests`, `psycopg`, `PyYAML`. Adding a fourth needs a reason; the cron
  parser is hand-written for exactly this reason.
- Credentials are never logged, committed, or baked into the image. Config
  dataclasses redact them from `repr()`; keep it that way when adding fields.
- Schedules are **UTC**. TNS stages hourly files on UT hours.
- Tests on the unit path touch no network and no database. Postgres tests are
  marked `integration` and skip without `TNS_TEST_DSN`.
- Any security suppression is registered in `security/waivers.yml` with a
  justification, an owner, and an expiry. CI fails when the expiry passes.
