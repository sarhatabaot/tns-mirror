# tns-mirror-server

The sync daemon: authenticates to TNS, downloads the public objects catalogue,
and writes it into a Postgres database it owns. Distributed as a **Docker
image**, not on PyPI.

It does not query the catalogue on anyone's behalf — no cone search, no
cross-matching. That is [`tns-mirror-client`](../client), and the
[schema](../schema) is the contract between them.

```console
$ tns-mirror-server migrate            # create/upgrade the schema
$ tns-mirror-server sync               # daily full snapshot
$ tns-mirror-server sync --hour 14     # one hourly delta
$ tns-mirror-server catch-up 24        # repair a 24-hour gap
$ tns-mirror-server serve              # run on the internal schedule
$ tns-mirror-server status --max-age-hours 26
$ tns-mirror-server print-grants --database tnsdb
```

See the [repository README](../README.md) for setup, configuration, and the TNS
credential you must supply. Full design rationale is in `design-tdd.md`.

## Layout

| Path | What it is |
|---|---|
| `src/tns_mirror_server/ports/` | `Source` and `Store` protocols — all the engine depends on |
| `src/tns_mirror_server/adapters/` | TNS download/parse, Postgres store, in-memory fakes |
| `src/tns_mirror_server/engine.py` | full replace / delta upsert / catch-up. No I/O of its own |
| `src/tns_mirror_server/schema/` | the migrations, and therefore the contract |
| `tests/` | no network and no database on the unit path |

## Tests

```console
$ uv sync --all-groups
$ uv run pytest                        # unit tests only
$ TNS_TEST_DSN=postgresql://... uv run pytest -m integration
```
