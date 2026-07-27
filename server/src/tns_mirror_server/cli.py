"""``tns-mirror-server`` — the command-line entrypoint.

Design §5.4 and A.6. One-shot commands are the primitive:

    tns-mirror-server migrate           apply schema migrations
    tns-mirror-server sync              full snapshot (daily)
    tns-mirror-server sync --hour HH    one hourly delta
    tns-mirror-server catch-up [N]      trailing N-hour delta window (default 24)
    tns-mirror-server serve             long-lived process with an internal scheduler
    tns-mirror-server status            row count and freshness
    tns-mirror-server print-grants      SQL for the read-only consumer role

"Build the image once, deploy two ways": compose runs ``serve``; Kubernetes runs
CronJobs against the one-shot commands.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from . import __version__
from .adapters import PostgresStore
from .config import Config, load_config
from .engine import catch_up, delta_sync, full_sync
from .errors import TnsMirrorError
from .ports.source import Source, get_source
from .ports.store import Store
from .scheduler import CronSpec, Job, Scheduler

log = logging.getLogger("tns_mirror_server")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STALE = 3


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


def build_source(config: Config) -> Source:
    """Construct the configured source, refusing early without a credential.

    Only the commands that actually download call this, so ``migrate`` and
    friends stay usable on a mirror whose TNS credential has not been set yet —
    while ``sync``, ``catch-up`` and ``serve`` still fail at startup rather than
    at the first scheduled download hours later.
    """
    config.auth.validate()
    return get_source(config.source)(config)


@contextmanager
def _store(config: Config) -> Iterator[Store]:
    with PostgresStore.connect(config) as store:
        yield store


# --- commands ---------------------------------------------------------------


def cmd_migrate(config: Config, args: argparse.Namespace) -> int:
    with _store(config) as store:
        applied = store.migrate()
    if applied:
        print(f"applied: {', '.join(applied)}")
    else:
        print("schema already up to date")
    return EXIT_OK


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    source = build_source(config)
    reuse = bool(args.reuse_existing)
    with _store(config) as store:
        if args.hour is None:
            result = full_sync(store, source, reuse_existing=reuse)
        else:
            result = delta_sync(store, source, args.hour, reuse_existing=reuse)
    print(result.describe())
    return EXIT_OK


def cmd_catch_up(config: Config, args: argparse.Namespace) -> int:
    source = build_source(config)
    hours = args.hours if args.hours is not None else config.schedule.catchup_window_hours
    with _store(config) as store:
        result = catch_up(store, source, hours)
    print(result.describe())
    return EXIT_OK


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    with _store(config) as store:
        rows = store.count()
        meta = store.meta()

    print(f"schema_version:       {meta.schema_version}")
    print(f"rows:                 {rows}")
    print(f"last_full_sync_at:    {meta.last_full_sync_at or 'never'}")
    print(f"last_delta_sync_at:   {meta.last_delta_sync_at or 'never'}")
    print(f"last_source_snapshot: {meta.last_source_snapshot or 'never'}")

    if args.max_age_hours is None:
        return EXIT_OK

    # Freshness, not merely liveness: a row count alone looks identical whether
    # the mirror synced an hour ago or died three weeks ago.
    latest = max(
        (t for t in (meta.last_full_sync_at, meta.last_delta_sync_at) if t is not None),
        default=None,
    )
    if latest is None:
        print("STALE: no sync has ever completed", file=sys.stderr)
        return EXIT_STALE
    age_hours = (datetime.now(UTC) - latest).total_seconds() / 3600
    if age_hours > args.max_age_hours:
        print(
            f"STALE: last sync was {age_hours:.1f}h ago (limit {args.max_age_hours}h)",
            file=sys.stderr,
        )
        return EXIT_STALE
    return EXIT_OK


def cmd_print_grants(config: Config, args: argparse.Namespace) -> int:
    """Emit least-privilege SQL for a consumer role (design §4.4).

    Deliberately does *not* include ``ALTER DEFAULT PRIVILEGES``: that would
    grant the reader SELECT on every future table in the schema, which is a
    wider grant than the one table (plus metadata) a consumer needs.

    No password is generated or printed — the SQL uses a psql variable so the
    operator supplies one out of band and it never lands in a shell history.
    """
    schema, table, role = config.database.schema, config.database.table, args.role

    connect = f"GRANT CONNECT ON DATABASE {args.database}"
    usage = f"GRANT USAGE ON SCHEMA {schema}"
    catalogue = f"GRANT SELECT ON {schema}.{table}"
    metadata = f"GRANT SELECT ON {schema}.tns_mirror_meta"
    width = max(len(connect), len(usage), len(catalogue), len(metadata))

    def grant(statement: str) -> str:
        return f"{statement:<{width}} TO {role};"

    print(
        f"""-- tns-mirror: least-privilege read-only consumer role.
-- Run as a superuser (or the database owner) against the mirror database.
-- Supply the password out of band, e.g.:
--   psql -v pw="$(openssl rand -base64 24)" -d {args.database} -f grants.sql

CREATE ROLE {role} LOGIN PASSWORD :'pw';

{grant(connect)}
{grant(usage)}

-- Exactly two objects: the catalogue, and the metadata a client needs to check
-- the schema version it was built against and how fresh the data is.
{grant(catalogue)}
{grant(metadata)}

-- Deliberately no ALTER DEFAULT PRIVILEGES: that would grant this role SELECT
-- on every table created in the schema later, which is wider than it needs.
-- Write credentials stay with the server and are never shared (invariant 5)."""
    )
    return EXIT_OK


def cmd_serve(config: Config, args: argparse.Namespace) -> int:
    """Long-lived process: migrate, seed if empty, then run the schedule."""
    source = build_source(config)

    with _store(config) as store:
        store.migrate()
        empty = store.count() == 0
    if empty:
        log.info("catalogue is empty; taking an initial full snapshot")
        with _store(config) as store:
            full_sync(store, source)

    schedule = config.schedule

    def run_full() -> None:
        with _store(config) as store:
            full_sync(store, source)

    def run_delta() -> None:
        with _store(config) as store:
            catch_up(store, source, schedule.catchup_hours)

    def run_catchup() -> None:
        with _store(config) as store:
            catch_up(store, source, schedule.catchup_window_hours)

    # A connection per run rather than one held open for the process lifetime:
    # a daemon's idle connection is the thing that silently dies overnight.
    wanted: list[tuple[str, str, Callable[[], None]]] = [
        ("full", schedule.full_cron, run_full),
        ("delta", schedule.delta_cron, run_delta),
        ("catch-up", schedule.catchup_cron, run_catchup),
    ]
    jobs = [
        Job(name=name, spec=CronSpec.parse(expr), run=run)
        for name, expr, run in wanted
        if expr.strip()
    ]
    if not jobs:
        log.error("every schedule is empty; nothing to do")
        return EXIT_ERROR

    scheduler = Scheduler(jobs)
    scheduler.install_signal_handlers()
    scheduler.run(max_iterations=args.max_iterations)
    return EXIT_OK


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tns-mirror-server",
        description="Mirror the TNS public objects catalogue into SQL.",
    )
    parser.add_argument(
        "--version", action="version", version=f"tns-mirror-server {__version__}"
    )
    parser.add_argument("--config", help="path to tns-mirror.yaml (default $TNS_MIRROR_CONFIG)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending schema migrations").set_defaults(
        func=cmd_migrate
    )

    sync = sub.add_parser("sync", help="download and ingest (full snapshot by default)")
    sync.add_argument(
        "--hour",
        type=int,
        choices=range(24),
        metavar="HH",
        help="ingest this UT hour's delta instead of the full snapshot",
    )
    reuse = sync.add_mutually_exclusive_group()
    reuse.add_argument(
        "--redownload",
        action="store_true",
        help="force a fresh download (the default; accepted for explicitness)",
    )
    reuse.add_argument(
        "--reuse-existing",
        action="store_true",
        help="ingest the already-downloaded file if present, without fetching (debugging)",
    )
    sync.set_defaults(func=cmd_sync)

    catchup = sub.add_parser("catch-up", help="apply a trailing window of hourly deltas")
    catchup.add_argument(
        "hours",
        nargs="?",
        type=int,
        default=None,
        help="window size, 1-24 (default: schedule.catchup_window_hours, 24)",
    )
    catchup.set_defaults(func=cmd_catch_up)

    serve = sub.add_parser("serve", help="run continuously on the configured schedule")
    serve.add_argument("--max-iterations", type=int, default=None, help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="row count and sync freshness")
    status.add_argument(
        "--max-age-hours",
        type=float,
        default=None,
        help="exit 3 if the last sync is older than this (for healthchecks)",
    )
    status.set_defaults(func=cmd_status)

    grants = sub.add_parser("print-grants", help="SQL for a read-only consumer role")
    grants.add_argument("--role", default="tns_ro", help="role name (default: tns_ro)")
    grants.add_argument("--database", default="tnsdb", help="database name (default: tnsdb)")
    grants.set_defaults(func=cmd_print_grants)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    try:
        config = load_config(args.config)
        return int(args.func(config, args))
    except TnsMirrorError as exc:
        # Deliberate failures print a single actionable line; a traceback here
        # would be noise, and could carry a DSN into the logs.
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
