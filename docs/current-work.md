# Current work and handoffs

The **live** record of meaningful active work, not a permanent history. Resolve
completed entries and keep lasting outcomes in `CHANGELOG.md`, the roadmap, the
architecture document, or an ADR. Do not assume a stale entry is abandoned. This
repository is public, so record nothing here that should stay private.

## Active tasks

### Task: mirror the pre-2016 historic supernovae

- **Status:** In progress
- **Owner:** Not recorded
- **Branch/worktree:** `historic-sne`
- **Scope:** Schema v1.1 (`tns_historic_sne`), a paged search-page source,
  `sync-historic`, a weekly job in `serve`, and the version bump to 1.1.0
- **Current state:** Committed as a work-in-progress commit. Server unit tests,
  Ruff, and the version and schema drift checks passed at that commit. The Postgres
  integration tests are not confirmed to have run. The client and API suites were
  not run.
- **Blockers:** None recorded
- **Next step:** Run the Postgres integration tests, then add the typed client
  accessor that `CHANGELOG.md` lists as a follow-up, or decide to ship without it.

## Suggested task entry

### Task: <short title>

- **Status:** Planned / In progress / Blocked / Ready for review
- **Owner:** If known
- **Branch/worktree:** If relevant
- **Scope:** Components and files involved
- **Current state:** What has been done and verified
- **Blockers:** Unresolved issues or approval needed
- **Next step:** Concrete action for the next agent
