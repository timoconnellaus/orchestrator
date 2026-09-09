#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
MODE=${1:-}

case "$MODE" in
control | livekit | voice) ;;
*)
  echo "Usage: $0 control|livekit|voice" >&2
  exit 2
  ;;
esac

cd "$ROOT"
exec node "$ROOT/scripts/run.mjs" "$MODE"
