#!/usr/bin/env python3
"""Render the published schema contract from the packaged migrations.

The migrations are the source of truth; ``schema/schema_v1.sql`` is the flattened
contract consumers read — including consumers in other languages, who will never
run this codebase. Keeping the two in step by hand would guarantee drift, so CI
runs this with ``--check`` and fails if the published file is stale.

    python scripts/render_schema.py            # write schema/schema_v1.sql
    python scripts/render_schema.py --check    # fail if it is out of date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server" / "src"))

from tns_mirror_server import SCHEMA_VERSION  # noqa: E402
from tns_mirror_server.migrator import load_migrations  # noqa: E402

HEADER = f"""\
-- ---------------------------------------------------------------------------
-- tns-mirror published schema contract, version {SCHEMA_VERSION}
--
-- GENERATED FILE — do not edit. Produced from server/src/tns_mirror_server/schema/
-- by server/scripts/render_schema.py; CI fails if this file drifts from them.
--
-- This is the whole interface. Consumers read `tns_objects` and `tns_mirror_meta`
-- through a read-only role (see `tns-mirror-server print-grants`) and share no
-- code with the server. Any language with a Postgres driver is a first-class
-- consumer.
--
-- Versioning: a breaking change (rename or drop a column, change a type or unit)
-- bumps the major and is announced in CHANGELOG.md. An additive change (a new
-- nullable column, a new index) bumps the minor and never breaks a reader.
-- ---------------------------------------------------------------------------

"""


def render() -> str:
    parts = [HEADER]
    for migration in load_migrations():
        parts.append(f"-- >>> {migration.version}\n")
        parts.append(migration.render("public", "tns_objects").rstrip() + "\n\n")
    return "".join(parts).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args()

    target = REPO_ROOT / "schema" / f"schema_v{SCHEMA_VERSION}.sql"
    rendered = render()

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != rendered:
            print(
                f"{target.relative_to(REPO_ROOT)} is out of date.\n"
                f"Run: python server/scripts/render_schema.py",
                file=sys.stderr,
            )
            return 1
        print(f"{target.relative_to(REPO_ROOT)} is up to date")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
