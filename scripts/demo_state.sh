#!/usr/bin/env bash
# Restore KeepBook's backend to a known demo state (T42/T43).
#
# Usage: scripts/demo_state.sh seed|fallback
#
#   seed     -> backend/state.demo.json     (Ruth Okafor one confirm from
#               complete, Marcus Whitfield missing 1099-INT, Chen Partnership
#               missing K-1 + 1098 entirely — see docs/USER-JOURNEY.md)
#   fallback -> backend/state.fallback.json (same session fully processed,
#               for instant recovery if live processing stalls on stage)
#
# main.py only loads state.json at startup (backend/main.py _load_state(),
# called from the FastAPI "startup" event) — there is no hot-reload endpoint.
# So this script copies the chosen state file into place, stages the images
# it references (uploads/ is gitignored, so they aren't in git — this script
# copies them fresh from eval/ every time), then restarts uvicorn on :8100.

set -euo pipefail

MODE="${1:-}"
if [[ "$MODE" != "seed" && "$MODE" != "fallback" ]]; then
  echo "usage: $0 seed|fallback" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
EVAL_DIR="$REPO_ROOT/eval"
UPLOADS_DIR="$BACKEND_DIR/uploads"
PORT=8100

case "$MODE" in
  seed)     SRC_STATE="$BACKEND_DIR/state.demo.json" ;;
  fallback) SRC_STATE="$BACKEND_DIR/state.fallback.json" ;;
esac

if [[ ! -f "$SRC_STATE" ]]; then
  echo "missing $SRC_STATE" >&2
  exit 1
fi

mkdir -p "$UPLOADS_DIR"

# doc_id:path-relative-to-eval/ manifest for every image either state file
# references. Portable (no bash4 associative arrays — macOS ships bash 3.2).
IMAGE_MANIFEST="
doc_001:testset/1099int_clean_01.png
doc_002:testset/1098_clean_01.png
doc_003:testset/w2_clean_02.png
doc_004:w2_test.png
doc_005:testset/receipt_01.png
doc_006:testset/1099int_clean_02.png
doc_007:testset/k1_clean_01.png
"

echo "staging images into $UPLOADS_DIR ..."
while IFS=: read -r doc_id rel; do
  [[ -z "$doc_id" ]] && continue
  src="$EVAL_DIR/$rel"
  dst="$UPLOADS_DIR/${doc_id}.png"
  if [[ -f "$src" ]]; then
    cp "$src" "$dst"
  else
    echo "  WARNING: source image missing, skipped: $src" >&2
  fi
done <<< "$IMAGE_MANIFEST"

cp "$SRC_STATE" "$BACKEND_DIR/state.json"
echo "backend/state.json <- $(basename "$SRC_STATE")"

# Restart uvicorn on :8100 so the new state actually loads.
EXISTING_PIDS="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"
if [[ -n "$EXISTING_PIDS" ]]; then
  echo "stopping existing server on :$PORT (pid $EXISTING_PIDS)"
  kill $EXISTING_PIDS
  for _ in $(seq 1 40); do
    lsof -ti "tcp:$PORT" >/dev/null 2>&1 || break
    sleep 0.25
  done
fi

cd "$BACKEND_DIR"
nohup .venv/bin/python -m uvicorn main:app --port "$PORT" \
  > "$BACKEND_DIR/uvicorn.log" 2>&1 &
disown
NEW_PID=$!

echo "uvicorn restarting on :$PORT (pid $NEW_PID), log: $BACKEND_DIR/uvicorn.log"
echo "next steps:"
echo "  1. wait ~1-2s for startup"
echo "  2. curl -s http://localhost:$PORT/clients | python3 -m json.tool"
echo "  3. curl -s http://localhost:$PORT/documents | python3 -m json.tool"
