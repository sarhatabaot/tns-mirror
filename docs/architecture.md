# Architecture

The current shape of the repository. The rules that bind it are in
[`CLAUDE.md`](../CLAUDE.md), the table contract is in
[`schema/README.md`](../schema/README.md), and module-level detail for the server
is on the [development page](src/development.md).

## Purpose and scope

tns-mirror keeps a SQL copy of the public catalogue of the Transient Name Server
(TNS). It does not interpret that catalogue: deciding what a TNS name means is left
to whoever reads it.

## Components

| Package | Ships as | Role | Database access |
|---|---|---|---|
| `server/` (`tns-mirror-server`) | Docker image | Downloads from TNS, authenticates, writes | Sole writer |
| `client/` (`tns-mirror-client`) | PyPI library | Typed reads and the cone search | Read-only role |
| `api/` (`tns-mirror-api`) | Docker image, optional | Read-only HTTP transport in front of the client | Read-only role |

Other top-level directories:

- `schema/` is the published contract, generated from the server's migrations.
- `quickstart/` holds the Compose deployments, including the API gateway.
- `docs/src/` is the published documentation site.
- `scripts/` and `security/` hold the repository's gates and security waivers.

## Boundaries and data flow

```text
TNS ──> server ──writes──> PostgreSQL <──reads── client <── api <── HTTP callers
                                          ▲
                                          └── any consumer, through plain SQL
```

- **The schema is the only contract between the server and everything that reads
  the database.** The server shares no code with the client or the API.
- **The API depends on the client,** so the cone search has one implementation.
- **Server internals are ports and adapters.** `engine.py` depends only on the
  protocols in `ports/`, and only `adapters/` touch the network, the disk, or SQL.

## Operational constraints

- A failed or rate-limited sync leaves the last good snapshot queryable, and the
  full refresh is one transaction.
- Requests to TNS are always authenticated.
- The server's write credentials stay with the server, and the database is not
  internet-exposed.
- The major version is the schema version across all three packages.

## Related decisions

See [`decisions/`](decisions/README.md).
