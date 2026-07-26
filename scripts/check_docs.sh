#!/usr/bin/env bash
# Build the documentation site, so a broken template is caught before it is
# pushed rather than by the Pages deploy.
set -euo pipefail

cd "$(dirname "$0")/../docs"

if [ ! -d node_modules ]; then
    echo "installing documentation dependencies…"
    npm ci --no-audit --no-fund
fi

npm run build --silent
echo "documentation site builds"
