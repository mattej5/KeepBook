# Eval Harness Spec

Top open item per PRD §9. This spec is deliberately concrete — build exactly this, no guessing needed. Extends `eval/gen_w2.py` + `eval/run_test.py`.

## Goal

Replace "one clean synthetic W-2" with a scored test set. Output two numbers judges can see: **doc-type classification accuracy** and **per-field extraction accuracy**, plus latency.

## Test set: 15–20 documents

| Bucket | Count | How to make |
|---|---|---|
| Clean synthetic W-2s | 3 | `gen_w2.py` with varied names/employers/amounts (parameterize the hardcoded values, seed randomness) |
| Clean synthetic 1099-NEC | 3 | New generator, same PIL approach: payer, recipient, TIN, box1 nonemployee comp |
| Clean synthetic 1099-INT | 2 | payer, recipient, box1 interest income |
| Clean synthetic K-1 | 2 | partnership name, partner name, EIN, ordinary income |
| Clean synthetic 1098 | 2 | lender, borrower, box1 mortgage interest |
| Phone photos | 3–4 | Print 3–4 of the above, photograph with real phone: one angled, one shadowed, one slightly blurry. This is the messy-doc evidence the PRD says we lack |
| Handwritten | 1 | Hand-fill a printed blank W-2 template, photograph |
| Unrecognized | 1–2 | A receipt or random letter — must classify as UNRECOGNIZED, not force-fit |

All data synthetic/fake. Never a real SSN.

## Labels

One file `eval/labels.json`:

```json
{
  "w2_clean_01.png": {
    "doc_type": "W-2",
    "fields": {
      "employee_name": "Marcus D. Whitfield",
      "ssn": "412-55-9083",
      "employer": "Cascade Logistics LLC",
      "box1_wages": "68420.15",
      "box2_fed_withheld": "9183.44"
    }
  },
  "receipt_01.png": {"doc_type": "UNRECOGNIZED", "fields": {}}
}
```

## Scoring rules

- **Doc type**: exact match after uppercasing. UNRECOGNIZED expected → any confident type = wrong.
- **Money fields**: strip `$`, commas, whitespace → compare as float, tolerance 0.01. `68,420.15` == `68420.15`.
- **Names/strings**: casefold, strip punctuation and extra whitespace, then exact match. Log near-misses for manual review rather than fuzzy-matching.
- **Missing field in model output**: wrong.
- Track **silent wrong values** separately (field present, well-formed, wrong) — that's the killer failure class from the e2b test and worth its own line in results.

## Runner: `eval/run_eval.py`

```
python run_eval.py --model gemma4:e4b --labels labels.json --docs ./testset/
```

Per doc: call the same prompt path the backend uses (import it, don't duplicate — eval must measure the production prompt), record parsed output + latency. Emit:

```
doc-type accuracy:   18/19 (94.7%)
field accuracy:      87/92 (94.6%)
silent wrong values: 3
median latency:      19.2s
```

Write full per-doc results to `eval/results.json` (goes in repo + writeup).

## Also run e2b

Same set through `gemma4:e2b` once. If it silently botches more money fields, the kill-test story scales from n=1 to n=19 — that's the strongest Evidence & Evaluation material we can buy for one command.

## Order of work

1. Generators + labels (biggest chunk, mechanical)
2. Runner + scoring
3. Run e4b, save results
4. Run e2b comparison
5. Print + photograph bucket last (needs printer/phone, do at office in morning)
