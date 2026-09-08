#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
for module in apps/control packages/worker-tools; do
  (cd "$ROOT/$module" && npm ci --no-audit --no-fund)
done
(cd "$ROOT/apps/voice" && uv sync --frozen)
(cd "$ROOT/apps/mobile" && flutter pub get)
node "$ROOT/scripts/init-local.mjs"
printf '\nDependencies installed. See README.md for starting the modules.\n'
