# AGENTS.md — tns-mirror development guide

This document defines engineering standards and operating rules for AI coding agents and contributors. **Python** is the primary language; **uv** manages Python versions, environments, dependencies, and project workflows.

**Core principle:** Understand before modifying; make focused, verifiable changes; protect existing work; and leave the repository ready for the next contributor. Apply requirements proportionally to the task. Project-specific rules extend this baseline, subject to higher-priority instructions and safety requirements.

**[`CLAUDE.md`](CLAUDE.md) holds this project's invariants and wins any conflict with this file.** Read it first, whichever agent you are. This file is the general operating baseline; the sections below note where the project deliberately differs from it.

# Part I — Engineering Standards

## 1. Python and repository structure

- Declare supported Python versions using `requires-python` in `pyproject.toml`; use syntax and APIs compatible with that range. Significant compatibility changes require approval.
- This repository holds three independent packages, `server/`, `client/` and `api/`, each with its own `pyproject.toml`, `uv.lock`, `src/` and `tests/`. Run tools from inside the package you are changing.
- The client supports Python 3.11 and up, the server and API 3.13 and up.
- Keep modules cohesive, interfaces understandable, and dependencies explicit. Separate domain logic from infrastructure and presentation where useful; avoid premature abstractions and unrelated refactors.
- Document significant architectural decisions in `docs/architecture.md` or `docs/decisions/`.

## 2. Code quality

- Write readable, idiomatic Python with meaningful names, clear control flow, and appropriate type annotations.
- Prefer standard-library tools (`pathlib`, context managers, dataclasses, enums, protocols) where they genuinely improve clarity.
- Avoid mutable default arguments, hidden global state, unnecessary inheritance, and broad exception suppression.
- Use **Ruff** for formatting and linting. Recommended baseline: `E`, `F`, `I`, `B`, `UP`, plus selected `SIM`, `C4`, and `RUF` rules. Each package's `pyproject.toml` goes further and also selects `W` and `S`; keep those.
- Annotate public interfaces and new nontrivial code. Prefer precise types; justify `Any` and suppressions. No type checker is configured or enforced in this repository yet.
- Do not weaken linting, type checking, or tests merely to obtain a passing result. Avoid repository-wide auto-fixes unrelated to the task.

## 3. uv, dependencies, and packaging

Use uv instead of mixing environment and dependency managers unless the project documents an exception.

```sh
uv sync
uv add <package>
uv add --dev <package>
uv remove <package>
uv run <command>
uv lock --check
uv build
```

- Before adding a dependency, consider the standard library and existing dependencies. Evaluate maintenance, compatibility, licensing, security, and operational cost. **Every new runtime dependency of the server needs explicit approval:** the published image's dependency set is deliberately small, which is why its cron parser is hand-written (see `CLAUDE.md`).
- `pyproject.toml` declares supported Python versions and compatible dependency constraints; `uv.lock` records exact resolved versions for development and CI. Commit `uv.lock` by default unless project policy states otherwise. Never hand-edit resolved lockfile contents.
- For published libraries, declare consumer-compatible dependency ranges independently of the development lockfile.
- The build backend here is `hatchling`; do not switch it. The server and API ship as images and only the client goes to PyPI. Run `uv build` when client packaging is relevant.
- Use `uv run --frozen` for checks that should not rewrite `uv.lock`; plain `uv run` updates a stale lockfile in place.

## 4. Reliability and security

- Validate untrusted inputs at system boundaries; use actionable errors. Catch exceptions where recovery, cleanup, translation, or reporting is meaningful. Keep programming errors visible during development.
- Protect persistent data from corruption and partial writes. Treat external files, network responses, and user input as untrusted.
- Never commit credentials, tokens, keys, or secrets, or expose them in logs and examples. Use appropriate configuration and secret handling.
- Do not perform destructive operations without authorization or overwrite unrelated user or agent changes.

## 5. Testing

- Use **pytest** by default. Behavioral changes require appropriate tests; bug fixes should have regression tests. TDD is encouraged but optional.
- Test meaningful edge cases and failure paths; prefer deterministic, isolated unit tests and reusable fixtures.
- Add integration, end-to-end, property-based, performance, or security tests when relevant. Coverage is useful but no universal percentage is mandated.
- Do not delete failing tests, weaken assertions, or introduce broad skips to disguise a defect.

## 6. Containers and deployment

`server/Dockerfile` and `api/Dockerfile` are the images. Keep what they already do:

- Multi-stage builds on **Wolfi** (`cgr.dev/chainguard/wolfi-base`), with no build tools, caches or unnecessary packages in the final stage.
- Base images pinned by **digest**. A bare digest is deliberate here and is stricter than a version tag; Dependabot updates it.
- A non-root runtime user, minimal privileges, and no secrets in images or layers.
- The narrow build contexts, `server/` and `api/`. Nothing inside an image can reach the repository root.

Verify that an image builds and runs after changing its Dockerfile or runtime dependencies (`scripts/smoke_test_local.sh`).

# Part II — Agent Operations

## 7. Initialization and orientation

**At the beginning of each task:**

1. Read this `AGENTS.md`.
2. Inspect the current Git branch, status, and uncommitted changes.
3. Check `docs/current-work.md` for active tasks, ownership, blockers, and handoffs.
4. Identify the relevant code, tests, and applicable project-specific rules.

**Read additional documentation as needed:** `README.md` for unfamiliar project purpose; `docs/architecture.md` for component/interface changes; `docs/src/development.md` for tooling; `schema/README.md` for the data contract; `docs/roadmap.md` for prioritization; and `docs/decisions/` when revisiting past decisions.

Do not assume unfamiliar changes are abandoned. Avoid loading unrelated documentation for small tasks.

## 8. Planning and autonomy

Workflow: **Orient → Plan → Implement → Verify → Document → Handoff**.

- Small, scoped tasks may proceed after orientation. Complex tasks require a written plan covering scope, affected components, approach, risks, verification, and approval points; record significant active plans in `docs/current-work.md`.
- Agents may autonomously implement routine requested work, refactor within scope, add tests, run tools, update documentation, add small justified dependencies, and make focused local commits.
- **Ask approval** before major architectural changes, consequential dependencies, breaking compatibility changes, destructive data/filesystem operations, production infrastructure changes, or significant scope expansion.
- Do not expand a task merely because additional improvements seem useful.

## 9. Implementation and verification

- Inspect existing code, tests, interfaces, and patterns before editing; avoid duplicate functionality and unrelated refactors.
- Make incremental changes, preserve behavior except where intentionally changed, and keep the repository recoverable.
- Standard verification commands, run per package (`server`, `client` or `api`):

```sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

- Repository-wide gates:

```sh
python3 scripts/check_versions.py
cd server && uv run --frozen python scripts/render_schema.py --check
uvx pre-commit run --all-files --hook-stage manual   # everything CI runs
```

- Postgres tests are marked `integration` and skip without `TNS_TEST_DSN`; say so when they did not run. A change to a migration or to `store_postgres.py` is not verified until they have (see `docs/src/development.md`).
- Targeted checks are fine during iteration; before completion run all applicable checks, using full-project checks when practical.
- Conditional checks: `uv lock --check` and `uv build` for dependencies/packaging; integration tests for migrations and store changes; security checks for sensitive code; image build/runtime checks for containers.
- Fix failures introduced by the task. Investigate and report pre-existing, unrelated, or environmental failures without silently expanding scope. Never claim an unrun check passed.
- If work remains incomplete, leave a safe state and record blockers, verification gaps, and concrete next steps.

## 10. Git and concurrent agents

- Inspect status before editing; preserve unrelated changes. Never reset, clean, discard, or overwrite unfamiliar work without authorization. Review diffs and stage only task-owned files.
- **Local commits are allowed but not required** for completed, verified tasks. Use focused, descriptive commits; do not amend another contributor's commits without authorization.
- Do not attribute AI tools or agents in commit messages or pull request descriptions: no `Co-Authored-By` trailer for an agent and no "Generated with" line.
- Incomplete checkpoint commits are permitted only when explicitly requested or necessary for a clearly identified recovery/handoff; label them as incomplete.
- **Explicit approval is required** for pushing, creating pull requests, merging, rewriting remote history, or other consequential remote operations.
- Prefer separate Git worktrees for concurrent agents, while supporting coordinated shared workspaces. Each worktree may have its own `.venv` and share uv's package cache.
- Coordinate ownership and changes to shared files such as `pyproject.toml` and `uv.lock`. Re-read shared files before edits; report conflicts rather than overwriting or automatically repairing another agent's work.
- Use `docs/current-work.md` for active tasks; identify owner (if known), status, branch/worktree, affected components, blockers, and next actions. Do not infer abandonment from inactivity. This is a public repository, so record nothing there that should stay private.

# Part III — Project Continuity

## 11. Documentation

Update documentation only when materially affected; do not create paperwork for trivial edits.

Do not hard-wrap prose in Markdown files, PR descriptions, or issue and review comments. Break lines only where the structure needs it, such as paragraphs, list items, tables, and code blocks. Unneeded line breaks stop text from taking the full width of the page.

| Location | Purpose |
| --- | --- |
| `docs/architecture.md` | Current components, boundaries, and interfaces |
| `docs/src/development.md` | Accurate setup, run, test, and debugging commands (published on the docs site) |
| `docs/decisions/` | Significant decisions, alternatives, rationale, and consequences (ADRs) |
| `docs/roadmap.md` | Milestones, priorities, and planned work |
| `CHANGELOG.md` | History of released and meaningful completed work |
| `docs/current-work.md` | Current active work, blockers, and handoffs; resolve entries when done |

`current-work.md` is a live state record, not permanent history. Move lasting outcomes to the appropriate permanent document when closing active work. Only `docs/src/` is published as the documentation site; the other files under `docs/` are for contributors.

## 12. Project-specific rules

`CLAUDE.md` holds the project's rules: the invariants, scope, architecture, and the procedures for changing the schema or the version. Read it before any change. There are no separate rule modules.

## 13. Definition of done and handoff

Before reporting completion, verify that:

- [ ] Requested behavior is implemented and unintended regressions avoided.
- [ ] Behavioral changes have appropriate tests; applicable checks were run.
- [ ] Introduced failures are fixed; pre-existing/environmental failures are reported.
- [ ] The diff contains no unintended changes; dependency and config changes are justified.
- [ ] Relevant architecture, decisions, roadmap, changelog, and active-work records are updated where warranted.
- [ ] Any local commit contains only intended work; unresolved risks and blockers are recorded.

**Final report:** Summarize changes, checks and results, significant documentation updates, commit ID (if any), and remaining limitations or next steps. Be concise, factual, and explicit about unverified work.
