# Classify-only eval bucket (T65) — VERIFIED (Jul 18, morning run)

Classify-only doc types (`extract: false`) are classified + human-confirmed with
zero field extraction. They are scored on **doc_type only**; `run_eval.py` skips
field scoring for any label carrying `"classify_only": true`, so these docs can
never register a silent-wrong.

## Samples

Rendered offline by `eval/gen_classify_only.py` (pure PIL, deterministic) into
`eval/testset/`, with `classify_only: true` label entries merged into
`eval/labels.json`:

| file | doc_type |
|---|---|
| `charitable_receipt_01.png` | `charitable receipt` |
| `w9_01.png` | `W-9` |
| `brokerage_stmt_01.png` | `brokerage statement` |

## Status: VERIFIED — orchestrator GPU run, Sat Jul 18 AM

`MODEL_RUNTIME=ollama run_eval.py --model gemma4:e4b --images w9_01.png,charitable_receipt_01.png,brokerage_stmt_01.png,receipt_01.png,letter_01.png --out results_classify_only.json` (uncontended GPU):

- **5/5 doc-type accuracy.** All three classify-only samples classified exactly
  (`W-9`, `CHARITABLE RECEIPT`, `BROKERAGE STATEMENT`).
- **UNRECOGNIZED discipline held at the model level**: both junk negatives
  (`receipt_01`, `letter_01`) still landed `UNRECOGNIZED` with the 18-type enum —
  the enum-growth force-fit risk did not materialize.
- 0 silent wrongs, `field accuracy 0/0` (fields skipped as designed),
  median latency 10.3s (single classify call — cheaper than extract types).
- Full output: `eval/results_classify_only.json` (committed).

Caveat, recorded honestly: n=3 synthetic samples on 3 of the 12 new types; the
other 9 types ship classification-untested (same mandatory-confirm gate applies).
