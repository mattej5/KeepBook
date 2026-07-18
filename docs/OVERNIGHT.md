# Morning Brief — Sat Jul 18 (supersedes the 2 AM version)

Everything below is committed and reproducible from `eval/results_final_*.json` and the docs. Claim labels: VERIFIED unless noted.

## The headline

**The product works, is demo-hardened, and has honest numbers with an improvement arc.** Full E2E verified in a real browser (intake → extract → red-pen correct → confirm → checklist inks → nerd telemetry), 34 tests green, demo seed + fallback states staged, `/health` stamps the serving git sha.

## Final numbers (29-doc set: 12 clean, 12 photo, 2 junk, 3 real handwritten)

| | e4b FAST (ships) | e4b CAREFUL (`REGION_PASS=1`) | e2b (comparison) |
|---|---|---|---|
| Doc-type | 29/29 | 29/29 | 29/29 |
| Fields | 66/106 (62.3%) | **98/106 (92.5%)** | 40/106 (37.7%) |
| Silent wrongs | 21 | **8** | 36 |
| Median latency | 17.7s | 28.2s | 13.0s |

- Improvement arc: raw baseline was **43.6%**; gated conditional preprocessing (photo bucket 26%→64%, clean untouched by sha-proof) took it to 62.3%; region-crop takes it to 92.5% at +10.5s/doc — failed the interactive-latency gate so it ships as a **mode**, not a default. Two flags, both gate-annotated: `REGION_PASS=1` (careful mode), `HAND_ENSEMBLE=1` (gates passed: agreement-precision 75% vs 25% on disputes, all hand fields always flagged, hand latency 1.9x; default off for demo latency).
- Rejected with evidence (negative results are in the writeup): blind re-ask (converted misses into well-formatted wrongs), e2b-classify cascade (single-resident-model swap cost), 12b (vision latency exceeds the 60s serving envelope — so e4b beats both neighbors on the metrics that matter: e2b = half accuracy/double silent-wrongs, 12b = can't make the envelope).
- Handwriting (Rachel's real pen strokes): 58% fields, misses are human-plausible — including the model reading the flat-topped 3 as a 5 in the SSN, **the exact misread predicted at import**. The story is in DEMO-SCRIPT ("The SSN story") — tell it.

## What changed since 2 AM

Review panel (independent Fable + Opus reviewers) → docs/IMPROVEMENTS.md → all demo-critical items FIXED and test-pinned: outages show red banners (never the honesty-feature copy), 60s bounded timeouts, honest queue completion, per-doc model trace one click away in Review, field labels/masking for all form types, images-only intake, worker crash-guard, `/health` with git sha. Demo seed (T42) + fallback (T43) live: `scripts/demo_state.sh seed|fallback`.

## Your morning checklist (in order)

1. **Courier bake-off** (~20 min, quiet machine): quit Chrome/heavy apps → `ollama stop gemma4:e4b; ollama stop gemma4:e2b` → E2B image test through `localhost:9100/v1` (both models already downloaded; key in backend/.env; ask Claude for the staged script) → if it answers, kill test + 5-doc subset with `MODEL_RUNTIME=courier MODEL_NAME="gemma4:e4b"` → record verdict in PRD §8 either way. Only a pass lets the writeup name Courier.
2. **Real phone photos** (T23): print or photograph-from-screen 2-3 testset docs, AirDrop in, tell Claude — import + label + rerun is automated.
3. **Wi-Fi-off E2E with real browser drag-drop** (T40; also closes T30's last gap). Server start: `cd backend && MODEL_RUNTIME=ollama .venv/bin/python -m uvicorn main:app --port 8100`; check `curl localhost:8100/health` shows the current sha.
4. **Three stopwatch dry-runs** (T50) with docs/DEMO-SCRIPT.md — the SSN story is scripted; optional beat to rehearse: flip `REGION_PASS=1` live on one doc ("careful mode") and watch a wrong field correct itself.
5. **1:00 PM freeze** → fill any remaining WRITEUP brackets (runtime line from step 1) → **Kaggle submit** (T51, your login) → repo sweep (T52) → logistics (T53) → **3:00 PM**.

Merge status: agent/vin-overnight → main [see git log — if not yet merged, Claude merges when you confirm the morning session is up].
