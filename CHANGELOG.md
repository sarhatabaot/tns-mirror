# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

**The major version is the schema version.** `v1.x.y` speaks schema v1 — the
`tns-mirror-server` image and the `tns-mirror-client` library alike — so a
consumer pinning `tns-mirror-client>=1,<2` is pinning the contract. A breaking
schema change bumps the major on both.

## [Unreleased]

## [1.0.4] — 2026-08-05

Ships the optional HTTP API, which 1.0.3 was meant to and did not: that tag was
cut while every file still said 1.0.2, so the release verified and stopped
before publishing anything. **1.0.3 does not exist on PyPI or Docker Hub** —
nothing was published under it, and the tag is left in place only so it is not
reused. Go from 1.0.2 to 1.0.4.

Schema is **unchanged at v1**. Nothing here touches the contract, so a consumer
pinning `tns-mirror-client>=1,<2` needs no action.

### Changed — one file to bump

- **`VERSION` at the repository root is now the source of the project version**,
  and the other seven files are derived from it:

      echo 1.0.5 > VERSION
      python3 scripts/check_versions.py --write

  Same shape as `render_schema.py` → `schema_v1.sql`: one source, a generator,
  CI failing on drift. The derived files keep literals rather than importing a
  shared module because the images build with a **narrow context** — `server/`
  and `api/`, not the repository root — so nothing inside them can read a
  top-level file at build time. Each artifact stays self-contained.
- The check now compares every file against `VERSION` rather than only against
  each other, so it names the file that drifted instead of reporting only that
  they disagree. It also covers the image tags pinned in the compose files and
  documentation, which previously drifted silently and left the quickstart
  telling people to pull a superseded image.
- `SCHEMA_VERSION` is deliberately **not** derived. It changes on its own rare,
  deliberate occasions, and a major bump without a schema change is exactly the
  mistake the major-equals-schema rule exists to catch.
- The pre-commit hook's file pattern did not include `VERSION`, so editing only
  the source file would not have run the check.

### Added — keyed, tiered HTTP access (deployment recipe)

For deployments where the outer reverse proxy belongs to someone else, and
access has to be granted, revoked and metered without involving them.

- **`quickstart/docker-compose.api-gateway.yml`** adds a small nginx gateway and
  a second API container in front of the mirror. An `X-API-Key` maps to a
  consumer name, the name selects a tier, and each tier is its own container
  with its own pool and ceilings. Every consumer uses one URL; changing a
  consumer's tier changes nothing they send.
- **Nothing outside the stack holds a key.** `quickstart/nginx/handoff.conf` is
  the entire integration handed to whoever runs the outer proxy: one `location`
  pointing at loopback, which never needs editing again — not to add a consumer,
  rotate a key, or change a limit.
- **Rate limiting keys on the API key, not the address.** Two projects behind one
  NAT get separate budgets and one project across a cluster shares a single
  budget, neither of which an address-based limit can express. It also removes
  any need to trust `X-Forwarded-For`, which is why the API containers now set
  `TNS_API_TRUST_PROXY=false` in this configuration.
- The API itself is **unchanged**: no key handling in a published image, which
  keeps its one secret the read-only DSN. The gateway logs each consumer's name
  and never the key.
- The capped tier's API port is not published when the gateway is in use, so
  there is no route around the key check.

### Fixed — documentation

- The reverse-proxy examples set `X-Forwarded-For` with
  `$proxy_add_x_forwarded_for`, which **appends** to whatever the client sent.
  The limiter charges the left-most entry, so at the edge a caller could keep
  that slot and mint a fresh bucket per request — defeating the point of
  `TNS_API_TRUST_PROXY`. Now `$remote_addr`, with a note on when appending is
  correct.

### Added — `tns-mirror-api` (Docker Hub, optional)

An optional read-only HTTP service in front of a mirror, for consumers who
cannot reach Postgres — a browser, another language, a network where only HTTP
crosses the boundary. Litestar on granian.

- **A transport, not a second implementation.** Every query goes through
  `tns-mirror-client`, so the cone-search geometry exists once in the project
  rather than once per consumer. A test asserts a search across the 0/360 seam
  works through HTTP, and it passes purely because the client handles it.
- **A separate image on purpose.** The sync server holds write credentials and
  is not exposed; this holds the read-only role, so an exploit here reaches
  something that can `SELECT` two tables. It sets its session read-only as well,
  so a bug cannot write even through an over-privileged role.
- `GET /v1/nearest`, `/v1/cone`, `/v1/objects/{name}`, `/v1/objid/{objid}`,
  `/v1/meta`, plus `/healthz`, `/readyz` and browsable OpenAPI at `/docs`.
- **Per-caller rate limiting**, generous by default, with `RateLimit-*` headers.
  `TNS_API_TRUST_PROXY` is off by default: with it on and no proxy in front, any
  caller can spoof `X-Forwarded-For` and mint a fresh bucket per request.
- **`TNS_API_ROOT_PATH`** mounts every route under a prefix for running behind a
  reverse proxy. The OpenAPI `servers` entry and the container healthcheck follow
  it, so `/docs` "try it" works and a mounted container does not report unhealthy
  while serving correctly.
- **Radius and limit are clamped, not rejected** — rate limiting alone does not
  stop one enormous query, and an error the caller must handle is worse than the
  largest answer the service will give.
- `/readyz` returns 503 when the mirror is stale; `/healthz` does not touch the
  database. Restarting does not fix a stale mirror, so liveness must not depend
  on it.
- `scripts/api_demo.sh` brings the whole thing up against a throwaway seeded
  database for looking at the OpenAPI page — no TNS credential, no real mirror.

Two faults were caught before it ever ran anywhere: the connection pool ran its
`SET SESSION CHARACTERISTICS` on a non-autocommit connection, leaving every
connection `INTRANS` so the pool discarded them and the service would never have
started; and the route parameters used a Litestar style deprecated in 2.24 and
removed in 3, surfaced only because warnings are errors in the test suite.

## [1.0.2] — 2026-07-27

Bug-fix release. Both faults were hit setting up a fresh mirror from the
published 1.0.1 image.

### Fixed

- **The compose files could not connect to their own database.** They built a
  `DATABASE_URL` by string-substituting the password into a URL. A password is
  arbitrary bytes and a URL is not: `openssl rand -base64 24` — which the
  documentation told you to use — emits a `/` about 39% of the time, and a `/`
  ends a URL's authority section, so the password became part of the host and
  port and the server reported `failed to resolve host 'tns_writer'`. A `@`
  broke it differently. Both compose files now pass discrete `PGHOST`/`PGUSER`/
  `PGPASSWORD`/`PGDATABASE` variables, which no character in a password can
  reinterpret. `DATABASE_URL` is still supported for hand-picked passwords.
- **`migrate`, `status` and `print-grants` demanded a TNS credential** they never
  use. Invariant 4 is that the *download* refuses without one, not that every
  command does — and `print-grants` is how you create the read-only role, so
  needing a TNS account to print SQL against a local database blocked the
  documented setup order. Auth is now checked when a source is constructed, so
  `sync`, `catch-up` and `serve` still fail at startup rather than mid-run.

### Added

- **`quickstart/docker-compose.minimal.yml`** — the same setup with nothing
  optional in it, using a TNS bot. Four values to replace.
- **TNS's rejections are now readable.** A wrong marker or a mistyped bot key is
  the commonest real failure, and it surfaced as an unhandled traceback ending
  in `401 Client Error`, which says nothing about what to change. A 401 or 403
  now names the settings to check for the mode in use, and a 404 distinguishes
  an hour TNS has not staged yet from a wrong `TNS_URL`. A 429 is deliberately
  left untranslated — the engine needs the original error to stop a catch-up
  early. Network failures no longer produce a traceback either.
- **Search uses Pagefind's Component UI**, which Pagefind recommends over the
  Default UI for new integrations — a self-contained custom element with arrow
  key navigation, a `mod+k` shortcut and the ARIA combobox roles handled for us.
  Its stylesheet has no `prefers-color-scheme` rules, so its hard-coded light
  defaults rendered white-on-white in dark mode; it is now themed by mapping its
  `--pf-*` properties onto the site's tokens, taking dropdown text from a 1.0:1
  contrast ratio to 11.8:1.
- **A copy button on every code block** in the documentation. Blocks with shell
  prompts copy only the commands, without the `$` and without the example
  output, since pasting the output back would run it.
- **A pre-release smoke test** (`scripts/smoke_test.sh`). Everything else in CI
  tests the source; this drives the *built image* with the compose file we
  publish, against a real Postgres, in the order the documentation prescribes.
  Both faults above passed every source-level test and reached users.

  Two choices in it are load-bearing. The password contains `/`, `@`, `+` and
  `=` — with the shipped `CHANGE-ME` default the URL fault does not reproduce,
  so a smoke test using a well-behaved password would have passed while the
  release was broken. And no TNS credential is configured, because the local
  commands must work without one *and* the downloading commands must refuse.

  It gates every image build and, in the release workflow, sits between
  verification and publishing: `verify → smoke → publish`. Run it locally with
  `uvx pre-commit run smoke-test --hook-stage manual`.

  Verified by reverting each fault in turn and confirming it fails.

## [1.0.1] — 2026-07-26

Release plumbing only — no change to the server, the client, or the schema.

- The PyPI publish tolerates a version that is already present, so a release
  whose targets fail independently can be re-run rather than needing a version
  burned to complete it.
- The Docker job checks its registry credentials before building, instead of
  failing at login after a multi-architecture build.
- Pages is no longer deployed from the release. `docs.yml` already deploys it
  from `main`, which is where releases are cut from.

`1.0.0` reached PyPI but never reached Docker Hub; use `1.0.1` for the image.

## [1.0.0] — 2026-07-26

First release. Schema **v1**. Two artifacts from one tag:
`sarhatabaot/tns-mirror-server` on Docker Hub, `tns-mirror-client` on PyPI.

### `tns-mirror-server` (Docker Hub)

- **TNS source adapter** with two authentication paths, selected by
  `TNS_AUTH_MODE`: a registered account's `tns_marker` User-Agent on a `GET`, or
  a TNS bot's `api_key` submitted as form data on a `POST`. With neither
  configured the download refuses — there is no anonymous fallback.
- **Streamed, atomic download**: the archive is written to a temporary file and
  renamed into place, so an interrupted transfer never lands where the next run
  would read it as a catalogue.
- **Alias-tolerant CSV parser** covering the preamble, alternative column
  spellings (`RAdeg`, `DEJ2000`, `TNSName`, …), the `name_prefix` + `name` join,
  and per-row tolerance so one malformed line cannot fail a whole snapshot.
- **Sync engine**: daily full replace, hourly delta upsert on `objid`, and a
  trailing catch-up window applied oldest-to-newest, with a configurable throttle
  between downloads and an early stop on HTTP 429.
- **Postgres store**: the full snapshot commits as a single transaction, so a
  concurrent reader sees either the previous catalogue or the new one and
  de-published objects disappear on the swap.
- **Forward-only migration runner** with checksums, an advisory lock, and a
  refusal to proceed if a released migration was edited after it was applied.
- **Schema v1** — `tns_objects` (the twelve documented columns, `objid` primary
  key, `(ra, dec)` and `(name)` indexes) and `tns_mirror_meta` (schema version
  and sync freshness, written in the same transaction as the data).
- **CLI**: `migrate`, `sync [--hour HH]`, `catch-up [N]`, `serve`, `status`,
  `print-grants`.
- **`serve`** with a dependency-free UTC cron scheduler: daily full snapshot,
  hourly delta, and a daily 24-hour catch-up that repairs hours missed during
  downtime. A failing job is logged and the loop continues.
- **Docker image**: multi-stage Wolfi, digest-pinned bases, compilers only in the
  builder, non-root uid 10001, healthcheck on sync *freshness* rather than a row
  count. Compose brings up the server against a healthchecked Postgres with a
  read-only root filesystem and `no-new-privileges`.
- **CI**: ruff, unit tests, Postgres integration tests, published-contract drift
  check, released-migration immutability check, Gitleaks, Bandit, Semgrep,
  pip-audit, Trivy (filesystem and image), Hadolint, CodeQL, Dependabot, and a
  hard-failing waiver-expiry gate.

### `tns-mirror-client` (PyPI)

The schema, packaged as a Python API. Pin `tns-mirror-client>=1,<2`. Shares no
code with the server — the published contract is the only thing binding them.

- **`TnsMirror`** — a read-only client taking a DSN (or a connection you already
  manage): `nearest`, `search`, `by_name`, `by_objid`, `by_names`, `count`,
  `meta`. Results are plain dataclasses mirroring the schema columns exactly,
  plus a derived `separation_arcsec` on cone queries.
- **Correct cone search**, which is the reason to use a library rather than
  hand-written SQL. Four failure modes, each of which returns *fewer matches*
  rather than an error:
  - **Haversine, not the law of cosines.** `acos` of a dot product is
    ill-conditioned as the separation approaches zero — exactly where a
    cross-match works. An object matched against itself came back at ~3
    milliarcseconds; it now returns 0. Used in both the Python helper and the
    emitted SQL, so the two cannot disagree.
  - **Exact RA half-width**, `asin(sin(radius)/cos(dec))`. The familiar
    `radius/cos(dec)` under-covers as the cone widens.
  - **Pole handling** decided by `sin(radius) >= cos(dec)` rather than an
    arbitrary declination cut-off.
  - **0/360 seam** handled with two OR-ed ranges.
  - The prefilter box is padded by 1e-9° so an object sitting exactly on the
    search radius is not excluded by floating-point rounding.
- **Schema version check on connect**, raising `SchemaVersionError` rather than
  returning quietly wrong answers. Needs `SELECT` on `tns_mirror_meta`, which
  `print-grants` grants.
- **Freshness** via `meta().is_fresh()` — a row count cannot distinguish "synced
  an hour ago" from "died three weeks ago".
- **Read-only session** set on connect, so even a bug in the library cannot write
  through an over-privileged role.
- One dependency (`psycopg`), because this gets embedded in other people's
  applications. Python 3.11+, tested on 3.11, 3.12 and 3.13.
- **Contract test** in CI: the model's column names, order and nullability are
  checked against `schema/schema_v1.sql`. Integration tests seed a database from
  that same published file.

### Quickstart

- **[`quickstart/`](quickstart/)** — one `docker-compose.yml` that pulls the
  published image. Change two marked lines, `docker compose up -d`. No source
  checkout, no Python toolchain, no build step. Every value takes a
  `${VAR:-default}`, so a `.env` is an option rather than a requirement, and an
  optional YAML config is there for operators who prefer one.

### Release process

- **`release.yml`** — one tag ships both artifacts from one verified CI run:
  `sarhatabaot/tns-mirror-server` to Docker Hub (multi-arch, provenance, SBOM)
  and `tns-mirror-client` to PyPI (trusted publishing, build attestations).
  Nothing publishes until the full suite and the scanners pass.
- **`.pre-commit-config.yaml`** — the same gates CI runs, run locally. Fast checks
  on commit; Semgrep, pip-audit, Hadolint and the docs build on push.
- **Documentation site** — Eleventy with Liquid templates, styled with CUBE CSS,
  deployed to GitHub Pages. Full-width three-column layout, navigation grouped
  into Server / Client / Project so a reader who only consumes a mirror is not
  wading through operator documentation, light and dark themes from one set of
  tokens, build-time Prism highlighting, and static search via Pagefind.
- **`scripts/check_versions.py`** — the version is restated in five files and
  the major has to equal the schema version; drift is caught in pre-commit and
  again before anything publishes.
- The worked cone-search SQL in the README and the contract documentation uses
  haversine, matching exactly what the client emits.

### Notes on the design

Several points were under-specified in the design document and resolved here:

- **`source_snapshot`** is the UTC date of the TNS *file* that last wrote the row
  — daily snapshot or hourly delta — not of a daily snapshot specifically. It is
  `NOT NULL`, and a delta can insert an object no daily snapshot has seen yet.
  A wrapped catch-up window dates its pre-midnight hours to the previous day.
- **Rows without an `objid`** are dropped on *both* paths, not only on deltas:
  `objid` is the primary key and the upsert target, so such a row cannot be
  stored. Duplicate `objid`s within one file resolve last-one-wins instead of
  aborting the refresh.
- **Column subsetting was dropped.** A configurable contract is not a contract:
  a client version-locked to schema v1 would fail against a subset database with
  an undefined-column error rather than a `NULL`. One schema, always.
- **The read-only grants exclude `ALTER DEFAULT PRIVILEGES`**, which would have
  granted the reader `SELECT` on every table created in the schema afterwards.
  `tns_mirror_meta` *is* granted, so a client can verify the schema version it
  was built against.
- **Freshness is exposed explicitly.** A row count cannot distinguish "synced an
  hour ago" from "died three weeks ago", so `tns_mirror_meta` carries sync
  timestamps and `status --max-age-hours` is the healthcheck.
- **The hourly schedule is one job with an explicit window** (`catchup_hours`,
  default 1), plus a separate daily 24-hour catch-up, replacing three
  inconsistent descriptions of the hourly cadence.
- **Cone search is not in the server.** It belongs to the client; the server only
  owns the `(ra, dec)` index, which is DDL and therefore contract.

[Unreleased]: https://github.com/sarhatabaot/tns-mirror/compare/1.0.4...HEAD
[1.0.4]: https://github.com/sarhatabaot/tns-mirror/releases/tag/1.0.4
[1.0.2]: https://github.com/sarhatabaot/tns-mirror/releases/tag/1.0.2
[1.0.1]: https://github.com/sarhatabaot/tns-mirror/releases/tag/1.0.1
[1.0.0]: https://github.com/sarhatabaot/tns-mirror/releases/tag/1.0.0
