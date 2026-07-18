#!/bin/bash
# Courier OS bake-off (T41) — run on a QUIET machine (quit Chrome/heavy apps first).
# Usage: ./scripts/courier_bakeoff.sh [preflight|sanity|killtest|subset|all]
# Only a PASS here permits naming Courier in the writeup/demo (PRD §8 rule).
# NOTE: adapter calls have a hard 60s timeout; first call after model load may
# time out once or twice while Courier pages the model in — sanity retries 3x.
set -u
cd "$(dirname "$0")/.."
PY=backend/.venv/bin/python
STEP="${1:-all}"

# model names as Courier knows them — check preflight's /models output and
# override if they differ: COURIER_E4B=<id> COURIER_E2B=<id> ./scripts/courier_bakeoff.sh
E4B="${COURIER_E4B:-gemma4:e4b}"
E2B="${COURIER_E2B:-gemma4:e2b}"

env_get() { $PY -c "import sys; sys.path.insert(0,'backend'); import model_runtime; import os; print(os.environ.get('$1',''))"; }

preflight() {
  echo "== preflight =="
  echo "-- freeing GPU memory (ollama stop both models) --"
  ollama stop gemma4:e4b 2>/dev/null; ollama stop gemma4:e2b 2>/dev/null
  ollama ps
  BASE="$(env_get COURIER_BASE_URL)"; KEY="$(env_get COURIER_API_KEY)"
  if [ -z "$BASE" ]; then echo "FAIL: COURIER_BASE_URL unset (backend/.env)"; exit 1; fi
  echo "-- Courier models at $BASE --"
  curl -s -m 10 -H "Authorization: Bearer $KEY" "$BASE/models" || { echo "FAIL: Courier unreachable"; exit 1; }
  echo
  echo "(if model ids above differ from $E4B/$E2B, rerun with COURIER_E4B=... COURIER_E2B=...)"
}

one_shot() { # $1=model  — small-model image sanity through the adapter, 3 tries
  local model="$1" try=1
  while [ $try -le 3 ]; do
    echo "-- attempt $try: $model image call via adapter --"
    MODEL_RUNTIME=courier $PY - "$model" <<'EOF' && return 0
import base64, sys, time
sys.path.insert(0, "backend")
import model_runtime
b64 = base64.b64encode(open("eval/w2_test.png","rb").read()).decode()
t = time.time()
out = model_runtime.extract(b64, 'Return STRICT JSON only: {"doc_type": "..."} for this document.', model=sys.argv[1])
print(f"[{time.time()-t:.1f}s] {out[:300]}")
sys.exit(0 if out.strip() else 1)
EOF
    try=$((try+1))
  done
  return 1
}

sanity() {
  echo "== sanity: $E2B image path through Courier =="
  if one_shot "$E2B"; then echo "SANITY PASS"; else echo "SANITY FAIL — record in PRD §8, stop here"; exit 1; fi
}

killtest() {
  echo "== kill test: $E4B on w2_test.png (expect box2 9,183.44) =="
  MODEL_RUNTIME=courier $PY - "$E4B" <<'EOF'
import base64, sys, time
sys.path.insert(0, "backend")
import model_runtime
b64 = base64.b64encode(open("eval/w2_test.png","rb").read()).decode()
prompt = """You are a tax-document intake assistant. Look at this image and return STRICT JSON only, no prose:
{"doc_type": "...", "employee_name": "...", "ssn": "...", "employer": "...", "box1_wages": "...", "box2_fed_withheld": "..."}
Use the exact values printed on the form."""
t = time.time()
out = model_runtime.extract(b64, prompt, model=sys.argv[1])
print(f"[{time.time()-t:.1f}s]")
print(out)
print()
print("KILL TEST:", "PASS (9,183.44 present)" if "9,183.44" in out else "FAIL — wrong/missing box2")
EOF
}

subset() {
  echo "== 5-doc subset eval: $E4B via Courier =="
  MODEL_RUNTIME=courier $PY eval/run_eval.py --model "$E4B" \
    --labels eval/labels.json --docs eval/testset/ \
    --images w2_clean_01.png,w2_photo_01.png,1099int_clean_01.png,1098_photo_01.png,k1_clean_01.png \
    --out eval/results_courier_subset.json
  echo
  echo "compare vs ollama e4b full-run baseline: 62.3% fields / 17.7s median (results_final_e4b.json)"
}

verdict_note() {
  cat <<'TXT'

== record the verdict in PRD §8 (either way) ==
Template:
  Courier bake-off (Jul 18): sanity <PASS/FAIL>, kill test <PASS 6/6 / FAIL>,
  5-doc subset <n/n fields, Xs median> vs ollama baseline. Verdict: <ships as
  named runtime / not named — Ollama remains the verified runtime>.
Remember: after the bake-off, the first Ollama call will reload the model
(slow) — warm it before the demo (T53).
TXT
}

case "$STEP" in
  preflight) preflight ;;
  sanity)    sanity ;;
  killtest)  killtest ;;
  subset)    subset ;;
  all)       preflight && sanity && killtest && subset; verdict_note ;;
  *) echo "usage: $0 [preflight|sanity|killtest|subset|all]"; exit 2 ;;
esac
