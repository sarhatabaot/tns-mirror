#!/usr/bin/env bash
# Audit both packages' locked dependency sets for known vulnerabilities.
#
# The server is pinned to its .python-version rather than whatever Python happens
# to be on PATH: a single-version lockfile exported under a different interpreter
# produces hashes that do not match.
set -euo pipefail

cd "$(dirname "$0")/.."

audit() {
    local package=$1
    local python=$2
    local requirements
    requirements=$(mktemp)
    # shellcheck disable=SC2064  # expand $requirements now, not at trap time
    trap "rm -f '$requirements'" RETURN

    echo "--- $package ---"
    uv export --project "$package" --frozen --no-dev --no-emit-project \
        --format requirements-txt > "$requirements"
    uvx --python "$python" pip-audit \
        --requirement "$requirements" \
        --strict \
        --disable-pip
}

audit server "$(cat server/.python-version)"

# The client supports 3.11+, so it is audited at its floor — that is the
# resolution consumers on the oldest supported Python will actually get.
audit client "3.11"
