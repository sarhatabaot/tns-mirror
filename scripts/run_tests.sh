#!/usr/bin/env bash
# Run one package's test suite, from inside that package.
#
# The `cd` matters. `uv run --project server pytest` keeps the *caller's*
# working directory, so pytest ignores the package's `testpaths` and its
# registered markers, then happily collects the other package's tests against
# the wrong virtualenv. Running from inside the package is what makes the hook
# test what it claims to.
set -euo pipefail

package=${1:?usage: run_tests.sh <server|client|api> [pytest args...]}
shift

cd "$(dirname "$0")/../$package"
exec uv run --frozen pytest -q "$@"
