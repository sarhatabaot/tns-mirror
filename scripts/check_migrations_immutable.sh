#!/usr/bin/env bash
# Refuse to commit an edit to an already-released migration.
#
# Migrations are forward-only. The server enforces this at deploy time by
# checksumming what it applied and refusing to start on a mismatch — but by then
# the bad file is already in a release. This catches it at the commit.
#
# "Released" means: present in HEAD. A migration you added in this same commit is
# still yours to edit.
set -euo pipefail

SCHEMA_DIR="server/src/tns_mirror_server/schema"

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "no commits yet — every migration is still new"
    exit 0
fi

changed=$(git diff --cached --diff-filter=MD --name-only HEAD -- "$SCHEMA_DIR" || true)

if [ -n "$changed" ]; then
    echo "error: these released migrations were modified or deleted:" >&2
    echo "$changed" | sed 's/^/  /' >&2
    cat >&2 <<'EOF'

Migrations are forward-only. Every deployment that already applied one recorded
its checksum, and will refuse to start if the file no longer matches.

Add a new numbered file in that directory instead, then regenerate the published
contract:

    uv run --project server python server/scripts/render_schema.py
EOF
    exit 1
fi

echo "no released migration was modified"
