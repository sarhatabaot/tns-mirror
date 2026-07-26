# tns-mirror

A self-hostable mirror of the [IAU Transient Name Server](https://www.wis-tns.org)
public objects catalogue, in SQL.

TNS publishes its full object catalogue as a daily CSV snapshot plus hourly
delta files. `tns-mirror` downloads them, authenticates properly, and keeps a
Postgres table in sync — so you can cone-search, cross-match, and join against
TNS **offline**, without putting a rate-limited public API on your hot path.

> **This is not an official TNS service.** It is an independently-operated mirror
> *of* the TNS public catalogue. TNS's data-use terms and citation requirements
> apply to the data — see [Attribution](#attribution).

## What's here

| Artifact | Ships as | Status |
|---|---|---|
| **[`tns-mirror-server`](server/)** | Docker image (`sarhatabaot/tns-mirror-server`) | v1.0 |
| **[`schema/`](schema/)** | the published SQL contract | v1 |
| **[`tns-mirror-client`](client/)** | PyPI package | v1.0 |

The **server** downloads and writes. It does not query on anyone's behalf: no
cone search, no cross-matching. Consumers read the database through a read-only
role, and the **schema is the contract** between them. The **client** is an
ergonomic, typed wrapper over that same contract — never a privileged path, and
sharing no code with the server.

## Quick start

You need a TNS account, because **the mirror never downloads anonymously**.
Register at [wis-tns.org](https://www.wis-tns.org/), then either note your
`tns_marker` User-Agent or create a bot under your profile's Bot Management.

Grab [`quickstart/docker-compose.yml`](quickstart/docker-compose.yml), change
the two marked lines — a database password and your TNS credential — and:

```console
$ docker compose up -d
```

It pulls the published image, so there is no source checkout, no Python
toolchain, and no build step. Prefer to keep credentials out of the file? Every
value takes a `${VAR:-default}`, so a `.env` beside it overrides anything. Full
walkthrough in [`quickstart/`](quickstart/).

On first start the server applies its migrations, takes an initial full
snapshot, and then follows the schedule. Check on it:

```console
$ docker compose exec server tns-mirror-server status
schema_version:       1
rows:                 168423
last_full_sync_at:    2026-07-26 00:04:11+00
last_delta_sync_at:   2026-07-26 11:10:38+00
last_source_snapshot: 2026-07-26
```

Then create a reader for your application:

```console
$ docker compose exec server tns-mirror-server print-grants --database tnsdb > grants.sql
$ docker compose exec -T db psql -U tns_writer -d tnsdb \
    -v pw="$(openssl rand -base64 24)" -f - < grants.sql
```

## Querying it

```console
$ pip install 'tns-mirror-client>=1,<2'
```

```python
import os
from tns_mirror_client import TnsMirror

with TnsMirror(dsn=os.environ["TNS_RO_DSN"]) as tns:
    hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
    if hit:
        print(hit.name, hit.type, hit.redshift, hit.separation_arcsec)
```

Any language with a Postgres driver works too — the schema is documented in
[`schema/README.md`](schema/README.md). A cone search needs no database
extension: a bounding-box prefilter on the `(ra, dec)` index, then an exact
great-circle test.

```sql
SELECT objid, name, type, redshift,
       2 * degrees(asin(least(1, sqrt(
           power(sin((radians("dec") - radians(:dec)) / 2), 2)
         + cos(radians(:dec)) * cos(radians("dec"))
         * power(sin((radians(ra) - radians(:ra)) / 2), 2)
       )))) AS separation_deg
FROM tns_objects
WHERE ra BETWEEN :ra_min AND :ra_max      -- bbox prefilter, uses the index
  AND "dec" BETWEEN :dec_min AND :dec_max
ORDER BY separation_deg, objid
LIMIT 1;
```

Four things to get right, each of which fails *silently*: use haversine rather
than the law of cosines (`acos` loses its precision exactly at the small
separations a cross-match cares about); compute the RA half-width as
`asin(sin(radius)/cos(dec))`, not `radius/cos(dec)`, which under-covers; drop the
RA bound when the cone reaches a pole; and OR two ranges at the 0/360 seam.
Getting these right in every consumer, in every language, is what
`tns-mirror-client` exists to do.

## Commands

```console
tns-mirror-server migrate            # create or upgrade the schema
tns-mirror-server sync               # daily full snapshot
tns-mirror-server sync --hour 14     # one hourly delta
tns-mirror-server catch-up 24        # trailing 24-hour delta window
tns-mirror-server serve              # run continuously on the schedule
tns-mirror-server status [--max-age-hours 26]
tns-mirror-server print-grants --database tnsdb
```

**Build the image once, deploy two ways.** `docker compose up` runs `serve` with
its internal scheduler. On Kubernetes, run the one-shot commands as `CronJob`s
and skip the scheduler entirely:

| Job | Schedule (UTC) | Command |
|---|---|---|
| full snapshot | `0 0 * * *` | `sync` |
| hourly delta | `10 * * * *` | `catch-up 1` |
| gap repair | `30 0 * * *` | `catch-up 24` |

Schedules are UTC because TNS stages its hourly files on UT hours; a local-time
schedule drifts off them twice a year.

## Configuration

Every setting can come from a YAML file, an environment variable, or both — the
environment always wins. See [`server/config/tns-mirror.example.yaml`](server/config/tns-mirror.example.yaml)
and [`server/.env.example`](server/.env.example).

| Variable | Default | |
|---|---|---|
| `TNS_AUTH_MODE` | `marker` | `marker` or `bot` |
| `TNS_USER_AGENT` | — | required in `marker` mode |
| `TNS_API_KEY`, `TNS_BOT_ID`, `TNS_BOT_NAME` | — | required in `bot` mode |
| `DATABASE_URL` | — | write credentials (or the standard `PG*` variables) |
| `TNS_SCHEMA`, `TNS_TABLE` | `public`, `tns_objects` | where the catalogue lives |
| `TNS_URL` | the public-objects zip | |
| `TNS_TIMEOUT`, `TNS_THROTTLE` | `120`, `10` | seconds |
| `TNS_WORKDIR` | `/var/lib/tns-mirror` | transient download area |
| `TNS_FULL_CRON`, `TNS_DELTA_CRON`, `TNS_CATCHUP_CRON` | `0 0 * * *`, `10 * * * *`, `30 0 * * *` | `serve` only; empty disables |

### Two ways to authenticate

TNS access is per-account, and the mirror supports both mechanisms so you can use
whichever your registration gives you:

- **`marker`** — a registered account's `tns_marker{...}` User-Agent, sent on a
  `GET`. Simplest.
- **`bot`** — a TNS bot's `api_key`, submitted as form data on a `POST`,
  alongside a marker naming the bot.

They differ in HTTP verb, not just in headers. **With neither configured the
download refuses** rather than falling back to an anonymous request.

## Security

- **No baked identity.** The image ships with no TNS credential. A `tns_marker`
  and a bot `api_key` identify a *specific account*; each deployer supplies their
  own. `.env.example` contains only obvious placeholders.
- **Split credentials.** The server holds write access to its own database.
  Consumers get a distinct read-only role with `SELECT` on two tables and nothing
  else — verified by an integration test that asserts the reader is denied
  `INSERT`, `UPDATE`, `DELETE`, and `DROP`.
- **Not internet-exposed.** The compose file does not publish the database port.
- **Hardened image.** Multi-stage Wolfi, digest-pinned, compilers only in the
  builder, runs as uid 10001, read-only root filesystem, `no-new-privileges`.
- **Layered scanning**, all gating the merge: Gitleaks, Bandit, Semgrep,
  pip-audit, Trivy (filesystem and image), Hadolint, plus CodeQL, Dependabot and
  secret push-protection. Every suppression carries a justification *and* an
  expiry, and [the expiry is enforced by CI](scripts/check_waivers.py).

## Documentation

Full documentation lives in [`docs/`](docs/) and builds with
[Eleventy](https://www.11ty.dev/) using Liquid templates, styled with
[CUBE CSS](https://cube.fyi/):

```console
$ cd docs
$ npm install
$ npm run dev          # http://localhost:8080
```

## Development

```console
$ cd server
$ uv sync --all-groups
$ uv run pytest                      # unit tests: no network, no database
$ uv run ruff check .

$ docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pg --name tns-pg postgres:16
$ TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
```

The server is [ports-and-adapters](server/src/tns_mirror_server/ports/): the sync
engine depends only on a `Source` and a `Store` protocol, both of which have
in-memory fakes, so the whole engine — atomic replace, idempotent deltas,
catch-up ordering, rate-limit backoff — is tested without touching anything real.

The client is a separate package with its own lockfile:

```console
$ cd client
$ uv sync --all-groups
$ uv run pytest                      # unit + the schema/client contract test
$ TNS_TEST_DSN=postgresql://postgres:pg@localhost:55432/postgres uv run pytest -m integration
```

### pre-commit

The same gates CI runs, run locally — so a red build is a surprise rather than a
routine:

```console
$ uvx pre-commit install --install-hooks
```

Fast checks run on every commit: formatting, lint, Bandit, Gitleaks, the unit
tests, the published-schema drift check, the released-migration immutability
check, and waiver expiry. The slower scanners run on push: Semgrep, pip-audit,
Hadolint, and the docs build. Run everything on demand with:

```console
$ uvx pre-commit run --all-files --hook-stage manual
```

## Attribution

This project redistributes the TNS public objects catalogue. If you use the data,
you must follow the TNS [data-use terms](https://www.wis-tns.org/) and cite TNS
as they require. The mirror's own code is MIT-licensed; **the catalogue is not
the mirror's to license**.

## Releases

One tag ships both artifacts, from one CI run, so the published pair is always a
combination that was tested together:

```console
$ git tag v1.0.0 && git push origin v1.0.0
```

- `sarhatabaot/tns-mirror-server` → Docker Hub (multi-arch, with provenance and
  an SBOM)
- `tns-mirror-client` → PyPI (trusted publishing, with build attestations)

The documentation site deploys separately, from `main`, whenever `docs/`
changes — releases are cut from `main`, so it is already current.

They version independently — the server tracks the project, the client tracks the
schema major — and nothing publishes until the full test suite and the scanners
pass.

## Versioning

**The major version is the schema version.** `v1.x.y` speaks schema v1 — image
and library alike — so pinning `tns-mirror-client>=1,<2` pins the *contract*,
which is the thing consumers actually depend on.

| Change | Bump |
|---|---|
| Rename or drop a column, change a type or a unit | **major**, both artifacts |
| New nullable column, new index, new feature | minor |
| Fixes | patch |

`scripts/check_versions.py` enforces that every file agrees and that the release
tag matches; it runs in pre-commit and again before anything publishes.

## Roadmap

- **v1.0** — the server (both auth modes, full snapshot + hourly deltas, the
  published schema, the read-only role, the Docker image) and the client (typed
  models, correct cone search). *(this release)*
- **Later** — optional q3c/pg_sphere spatial indexing; an optional HTTP read
  transport behind the same client API; additional bulk-CSV catalogues behind the
  `Source` port.

## License

[MIT](LICENSE).
