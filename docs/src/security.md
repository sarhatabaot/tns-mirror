---
layout: layouts/base.liquid
title: Security
summary: Split credentials, no baked identity, a hardened image, and layered scanning that gates the merge.
permalink: /security/
section: server
order: 4
tags: docs
---

## No baked identity

The published image contains **no TNS credential**. A `tns_marker` and a bot
`api_key` identify a *specific TNS account*, so shipping one would mean everyone
running the image was impersonating its author.

Each deployer registers their own account or bot and supplies it as
configuration. The repository's `.env.example` carries only obvious placeholders,
and Gitleaks plus secret push-protection guard against a real one slipping in.

There is no anonymous fallback: with no credential configured, the download
refuses.

## Split credentials

| Who | Gets |
|---|---|
| The server | **Write** access to its own database. Never shared |
| Every consumer | A distinct **read-only** role: `SELECT` on two tables, nothing else |
| The [HTTP API](/api/) | The same read-only role — never the writer's |

`print-grants` generates the reader. It deliberately omits
`ALTER DEFAULT PRIVILEGES`, which would grant `SELECT` on every table created in
the schema afterwards — wider than a consumer needs, and the kind of grant nobody
revisits.

It also generates no password: the SQL uses a psql variable, so the operator
supplies one out of band and it never reaches a shell history or a CI log.

That boundary is verified by an integration test, not just asserted — the test
creates the role, applies the grants, and confirms the reader is denied `INSERT`,
`UPDATE`, `DELETE` and `DROP`.

### The API is a separate container for this reason

The optional [HTTP API]({{ '/api/' | url }}) is the only component intended to
face a network, so it is the one most likely to be attacked — and it holds the
read-only role. An exploit there reaches something that can `SELECT` two tables
and nothing else, while the sync server's write credentials stay in a container
that is not exposed at all.

It also sets its database session read-only, so a bug in the service cannot
write even if it were handed an over-privileged role by mistake. And it is
optional: a deployment that does not need HTTP has no network-facing component.

## The database is not a public service

The compose file does not publish the database port. The mirror's database is an
implementation detail shared with named consumers, not something to expose.

## Image hardening

- Multi-stage **Wolfi** build; compilers and build tooling exist only in the
  builder stage.
- Base images are **digest-pinned**, so a rebuild cannot silently pick up a
  different image.
- Runs as **non-root** (uid 10001).
- Compose runs it with a **read-only root filesystem** and `no-new-privileges`.
- Debugging tools live in an ephemeral sidecar, never baked into the production
  image.

## Layered scanning

One scanner is one blind spot. Every one of these gates the merge:

| Layer | Tool |
|---|---|
| Secrets | Gitleaks, full history, plus GitHub secret push-protection |
| SAST | Bandit and Semgrep (`p/python`, `p/secrets`), plus CodeQL |
| Dependencies | pip-audit over the frozen lockfile, plus Trivy filesystem scan and Dependabot |
| Container | Hadolint, plus a Trivy image scan |

Semgrep's `p/django` ruleset is deliberately absent — there is no Django here,
and rules that cannot fire are noise that trains people to ignore the scanner.

pip-audit runs pinned to `.python-version` rather than the runner's ambient
Python: a single-version lockfile exported under a different interpreter produces
hashes that do not match.

The same gates run locally through
[pre-commit]({{ '/development/#pre-commit' | url }}), so a red build is a surprise
rather than a routine.

## Waivers expire

Every suppression anywhere in the repository — a Bandit `# nosec`, a Semgrep
`nosemgrep`, a Trivy ignore, a pinned-but-vulnerable dependency — is registered
in `security/waivers.yml` with a justification, a named owner, and an **expiry
date**.

A CI step fails once an expiry passes. That is the point: a waiver is a decision
made under particular circumstances, and it should have to be re-made when those
circumstances may have changed. A suppression without an expiry is a permanent
hole nobody revisits.

At present there is exactly one: Bandit's `B104` for the API binding `0.0.0.0`,
which is what running in a container means — the network namespace is the
isolation boundary, and binding loopback would make the service unreachable from
its own published port. It is overridable, and the shipped compose profile
publishes on `127.0.0.1` rather than every interface. Everything else is clean
with no suppressions.

## Small dependency tree

The server depends on three packages: `requests`, `psycopg`, and `PyYAML`. No web
framework, no ORM, no scheduler library — the cron parser is hand-written for
exactly this reason. Every dependency is attack surface in a published image, so
adding a fourth needs a reason.

The client carries one, `psycopg`, because it is embedded in other people's
applications and every dependency it takes becomes theirs. The API necessarily
carries more — a web framework and a server — which is a further argument for it
being a separate, optional image rather than a mode of the server.

## Reporting a vulnerability

Please report privately through the repository's security advisories rather than
opening a public issue.
