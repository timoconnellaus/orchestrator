#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
(cd "$ROOT/apps/control" && npm run typecheck && npm test && npm run build)
(cd "$ROOT/packages/worker-tools" && npm run typecheck && npm test)
(cd "$ROOT/apps/voice" && uv run --frozen ruff check . && uv run --frozen pytest)
(cd "$ROOT/apps/mobile" && flutter analyze && flutter test)
node --check "$ROOT/scripts/run.mjs"
node --check "$ROOT/scripts/init-local.mjs"
printf '\nAll automated checks passed (no paid provider calls or live worker mutations).\n'
