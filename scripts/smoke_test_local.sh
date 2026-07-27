#!/usr/bin/env bash
# Build the image from the working tree, then smoke test it.
#
# The wrapper exists so the pre-commit hook is a single entry point: in CI the
# image is built by a separate step, but locally you want "check what I have
# right now" to be one command.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "building sarhatabaot/tns-mirror-server:smoke from the working tree…"
docker build -q -t sarhatabaot/tns-mirror-server:smoke server >/dev/null

exec scripts/smoke_test.sh smoke
