#!/usr/bin/env python3
"""KeepBook eval runner — docs/EVAL.md.

Scores the PRODUCTION pipeline (backend/pipeline.run_pipeline) over a labeled
test set. It imports the backend's classify+extract code directly rather than
copying it, so the numbers measure exactly what the demo runs. It honors the
same MODEL_RUNTIME / .env as the backend (via backend/model_runtime).

    python run_eval.py --model gemma4:e4b --labels labels.json --docs ./testset/

Emits the summary block and writes eval/results.json.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time

# Import the backend's production pipeline (not a copy). Both pipeline.py and
# model_runtime.py live in backend/, so putting that dir on sys.path lets the
# `from model_runtime import extract` inside pipeline resolve too.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import base64  # noqa: E402


# ---------------------------------------------------------------------------
# Scoring primitives (docs/EVAL.md "Scoring rules")
# ---------------------------------------------------------------------------
_MONEY_RE = re.compile(r"^\$?\s*[\d,]+(?:\.\d+)?\s*$")


def is_money(label_value: str) -> bool:
    return bool(_MONEY_RE.match(str(label_value).strip()))


def norm_money(v: str):
    stripped = re.sub(r"[,$\s]", "", str(v))
    try:
        return float(stripped)
    except ValueError:
        return None


def norm_string(v: str) -> str:
    s = re.sub(r"[^\w\s]", " ", str(v))
    return " ".join(s.split()).casefold()


def score_field(expected: str, predicted) -> str:
    """Return 'correct' | 'wrong' | 'missing'.

    'missing' = model produced no value for the field (absent or empty).
    'wrong'   = a value was produced but it does not match (silent-wrong class).
    """
    if predicted is None or str(predicted).strip() == "":
        return "missing"
    if is_money(expected):
        a = norm_money(expected)
        b = norm_money(predicted)
        if b is None:
            return "wrong"
        return "correct" if abs(a - b) <= 0.01 else "wrong"
    return "correct" if norm_string(expected) == norm_string(predicted) else "wrong"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="KeepBook eval runner")
    ap.add_argument("--model", default="gemma4:e4b", help="model tag (sets MODEL_NAME)")
    ap.add_argument("--labels", default=os.path.join(_HERE, "labels.json"))
    ap.add_argument("--docs", default=os.path.join(_HERE, "testset"))
    ap.add_argument("--out", default=os.path.join(_HERE, "results.json"))
    ap.add_argument("--limit", type=int, default=0, help="score only the first N (0=all)")
    ap.add_argument("--images", default="", help="comma-separated filenames to restrict to")
    args = ap.parse_args()

    # Point the shared adapter at the requested model BEFORE importing it.
    os.environ["MODEL_NAME"] = args.model
    import pipeline  # noqa: E402  (import after env is set)

    with open(args.labels, "r", encoding="utf-8") as fh:
        labels = json.load(fh)

    names = sorted(labels.keys())
    if args.images:
        wanted = {n.strip() for n in args.images.split(",") if n.strip()}
        names = [n for n in names if n in wanted]
    # Only score labeled images that actually exist on disk.
    names = [n for n in names if os.path.exists(os.path.join(args.docs, n))]
    if args.limit:
        names = names[: args.limit]

    if not names:
        print("No labeled images found under", args.docs, file=sys.stderr)
        return 1

    runtime = os.environ.get("MODEL_RUNTIME", "ollama")
    print(f"Running eval: model={args.model} runtime={runtime} docs={len(names)}\n")

    per_doc = []
    doc_type_correct = 0
    field_correct = 0
    field_total = 0
    silent_wrong = 0
    latencies = []

    for name in names:
        path = os.path.join(args.docs, name)
        with open(path, "rb") as fh:
            img_b64 = base64.b64encode(fh.read()).decode()

        t0 = time.time()
        result = pipeline.run_pipeline(img_b64)
        dt = time.time() - t0
        latencies.append(dt)

        exp = labels[name]
        exp_type = str(exp.get("doc_type", "")).upper()
        pred_type = str(result.get("doc_type", "")).upper()
        type_ok = pred_type == exp_type
        doc_type_correct += int(type_ok)

        pred_fields = result.get("fields", {}) or {}
        field_results = {}
        for key, exp_val in (exp.get("fields") or {}).items():
            field_total += 1
            verdict = score_field(exp_val, pred_fields.get(key))
            if verdict == "correct":
                field_correct += 1
            elif verdict == "wrong":
                silent_wrong += 1  # present + well-formed but wrong == silent wrong
            field_results[key] = {
                "expected": exp_val,
                "predicted": pred_fields.get(key, None),
                "verdict": verdict,
            }

        per_doc.append({
            "file": name,
            "latency_s": round(dt, 2),
            "doc_type": {
                "expected": exp_type,
                "predicted": pred_type,
                "correct": type_ok,
            },
            "fields": field_results,
        })
        flag = "OK " if type_ok else "XX "
        print(f"  {flag}{name:26s} type {pred_type:12s} (exp {exp_type:12s}) {dt:5.1f}s")

    n = len(names)
    dt_pct = 100.0 * doc_type_correct / n if n else 0.0
    field_pct = 100.0 * field_correct / field_total if field_total else 0.0
    median_lat = statistics.median(latencies) if latencies else 0.0

    summary = {
        "model": args.model,
        "runtime": runtime,
        "docs_scored": n,
        "doc_type_correct": doc_type_correct,
        "doc_type_total": n,
        "doc_type_accuracy": round(dt_pct / 100, 4),
        "field_correct": field_correct,
        "field_total": field_total,
        "field_accuracy": round(field_pct / 100, 4),
        "silent_wrong_values": silent_wrong,
        "median_latency_s": round(median_lat, 2),
    }

    print()
    print(f"doc-type accuracy:   {doc_type_correct}/{n} ({dt_pct:.1f}%)")
    print(f"field accuracy:      {field_correct}/{field_total} ({field_pct:.1f}%)")
    print(f"silent wrong values: {silent_wrong}")
    print(f"median latency:      {median_lat:.1f}s")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "per_doc": per_doc}, fh, indent=2)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
