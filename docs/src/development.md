---
layout: layouts/base.liquid
title: Development
summary: How the server is put together, how to run the tests, and the rules a change has to respect.
permalink: /development/
section: project
order: 1
tags: docs
---

## Architecture

Ports and adapters, not a framework. The server is a headless batch job: it needs
a database driver and a scheduler, not Django.

| Module | Role |
|---|---|
| `ports/` | The `Source` and `Store` protocols. All the engine depends on |
| `adapters/` | The only modules that touch the network, the disk, or SQL |
| `engine.py` | Provider-agnostic orchestration. **No I/O of its own** |
| `store_postgres.py` | The only module that knows SQL or the table name |
| `schema/` | The migrations, and therefore the contract |

Every port has an in-memory fake, so the whole engine — atomic replace,
idempotent deltas, catch-up ordering, rate-limit backoff — is tested without
touching anything real.

### Scope

The server **downloads, authenticates, and writes**. That is all.

Cone search, cross-matching, and deciding what a TNS name *means* belong to the
client or to the consumer. If you find yourself adding geometry to the server, it
is in the wrong package. The one exception is the `(ra, dec)` index, which is DDL
and therefore part of the contract.

The same rule binds the [HTTP API]({{ '/api/' | url }}): it is a transport in
front of the client, and every query goes through it. Geometry added there would
be a third implementation of something whose edge cases fail silently, and the
project would find out from users rather than from tests.

## Running the tests

```console
$ cd server
$ uv sync --all-groups
$ uv run pytest              # unit: no network, no database
$ uv run ruff check .
```

The store integration tests need a real Postgres and skip without one:

```console
$ docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pg --name tns-pg postgres:16
$ TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
```

Each integration test gets its own Postgres schema, so they are isolated and the
`TNS_SCHEMA` setting is exercised at the same time.

### The API

```console
$ cd api
$ uv sync --all-groups
$ TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
```

Its tests drive the real application factory rather than assembling handlers by
hand — the connection pool, the rate limiter and the startup schema-version
check all live in the app, so testing the handlers alone would miss them.

To look at the running service without a mirror or a TNS credential:

```console
$ ./scripts/api_demo.sh
```

## pre-commit

The same gates CI runs, run locally:

```console
$ uvx pre-commit install --install-hooks
```

Fast checks — formatting, lint, Bandit, Gitleaks, the unit tests, the schema
drift check, the waiver expiry — run on every commit. The slower scanners —
Semgrep, pip-audit, Hadolint, the docs build — run on push. Run everything on
demand:

```console
$ uvx pre-commit run --all-files --hook-stage manual
```

## Changing the schema

The schema is the published contract, so changing it is a deliberate act.

1. Add a **new numbered file** in `server/src/tns_mirror_server/schema/`. Never
   edit a released one — the runner checksums applied migrations and refuses to
   start on a mismatch, and both pre-commit and CI reject the edit at review time.
2. Regenerate the published contract:

   ```console
   $ uv run --project server python server/scripts/render_schema.py
   ```

   CI fails if `schema/schema_v1.sql` drifts from the migrations.
3. Version it: additive (a new nullable column, a new index) is a **minor**;
   breaking (a rename, a drop, a type or unit change) is a **major**, in lockstep
   with the client, announced in the changelog.

## Invariants

A change must not violate these.

1. **The schema is the only contract.** The server is the sole writer; consumers
   read through a read-only role. No shared code.
2. **The mirror knows nothing about its consumers.** No consumer-specific fields,
   names, or logic. Generic TNS→SQL.
3. **Effectively-once, atomic refresh.** The full snapshot is delete-then-insert
   in one transaction. Do not split it. Deltas upsert on `objid` and are
   idempotent.
4. **Authenticated to TNS, never anonymous.** No fallback, ever.
5. **Least privilege.** Write credentials stay with the server. The database is
   not internet-exposed. The container runs non-root.
6. **Self-contained.** Own lockfile, `.python-version`, Dockerfile, CI. No reach
   into another repository.
7. **Fail safe.** A failed sync leaves the last good snapshot intact and
   queryable. A quiet delta hour is a no-op; only the full refresh is strict.

## Conventions

- Python ≥ 3.13, uv, hatchling, ruff for lint and format.
- Three runtime dependencies. Adding a fourth needs a reason.
- Credentials are never logged, committed, or baked into the image. Config
  objects redact them from `repr()`; keep it that way when adding fields.
- Schedules are UTC.
- Any security suppression is registered in `security/waivers.yml` with a
  justification, an owner, and an expiry.

## The documentation site

This site is [Eleventy](https://www.11ty.dev/) with Liquid templates, styled
using [CUBE CSS](https://cube.fyi/).

```console
$ cd docs
$ npm install
$ npm run dev      # http://localhost:8080
$ npm run build
```

**Styling.** The stylesheet is assembled from numbered layers in
`docs/src/_includes/css/` — Tokens, Global axioms, Compositions, Utilities,
Blocks, Syntax, Exceptions — and concatenated in that order, because in CUBE the
cascade order *is* the methodology. Layout lives in the compositions and knows
nothing about the components inside it; blocks stay small.

**Sections.** Each page declares `section: server | client | project` in its
front matter, and the sidebar groups by it. A reader is usually one or the other
— an operator running a mirror, or a consumer querying one somebody else runs —
so the navigation says which half is theirs rather than making them work it out.

**Theme.** Light and dark both come from the same tokens. The OS preference is
the default signal; an explicit choice is stored in `localStorage` and applied
before first paint, so navigating never flashes the wrong theme.

**Search** is [Pagefind](https://pagefind.app/)'s Component UI, which indexes the
built HTML after Eleventy runs. The index is static files, so search needs no
server and no third-party service. `npm run build` does both steps; `npm run dev`
builds once first so search works locally.

Pagefind's stylesheet carries no `prefers-color-scheme` rules — its defaults are
hard-coded light, so left alone it renders white-on-white for a dark-mode
reader. It is themed by mapping its `--pf-*` custom properties onto our tokens
in one place, which means both themes and the explicit toggle follow for free.

**Code highlighting** is Prism, run at build time by
`@11ty/eleventy-plugin-syntaxhighlight`. The markup is baked into the HTML, so no
highlighting JavaScript ships to the reader; only the token colours are ours.
