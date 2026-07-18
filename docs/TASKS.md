# Task Board — Sat Jul 18, submission 3:00 PM

Single source of truth for what's done and what's left. Humans and coding agents both work from this file.

**Rules for checking off a task (agents: this is binding):**
1. Flip `[ ]` → `[x]` ONLY after you personally ran the task's **Verify** step and observed it pass. Building it is not completing it.
2. Fill in **Evidence** with what you observed: the command you ran + decisive output line, and the commit hash if code changed. `Evidence: _none_` with a checked box is a violation.
3. If Verify fails, leave the box unchecked and append a `BLOCKED:` line explaining what you saw.
4. Never delete or reword a task's DoD to make it pass. If the DoD is wrong, add a note and flag a human.
5. Commit this file with the work it describes.

Owners: **V** = Vin, **agent** = any coding agent (with the owner reviewing).

---

## Phase 0 — Done before Saturday morning

- [x] **T01 — PRD + API contract + eval spec in repo** (V + agent)
  Evidence: commits `518b8e0`, `fa4c5f0`; PRD.md, docs/API.md, docs/EVAL.md on main.
- [x] **T02 — Labeled test set (26 images) + generators + augmenter** (agent)
  Evidence: commit `7075003`; `eval/testset/` 26 files, `eval/labels.json` cross-validated both directions.
- [x] **T03 — Model sources locked** (agent)
  Evidence: README Models section; `ollama pull gemma4:e4b` (9.6GB) / `e2b` (7.2GB) verified locally and on ollama.com registry; `gemma4:cloud` warning documented.
- [x] **T04 — Design reference in repo** (agent)
  Evidence: commit `7075003`; docs/design/ mockup HTML + full render + DESIGN.md.
- [x] **T05 — Dual-runtime design + user journey** (V + agent)
  Evidence: commits `8052649`, `5dcded1`; PRD §8, docs/API.md adapter contract, docs/USER-JOURNEY.md.
- [x] **T06 — Team registered; repo public** (V/agent)
  Evidence: Vin confirmed registration; `gh repo view higg22-git/KeepBook --json isPrivate` → `false`.

---

## Phase 1 — Backend core (owner V/agent; target ~11:00 AM)

- [x] **T10 — `backend/model_runtime.py` adapter** (V/agent)
  DoD: `extract(image_b64, prompt) -> str` implementing both shapes in docs/API.md, runtime/env-var selected; no other backend file contains a model URL.
  Verify: `MODEL_RUNTIME=ollama python -c "..."` returns non-empty model output for `eval/w2_test.png`; `grep -rn "11434\|api/generate\|chat/completions" backend/ --include="*.py" | grep -v model_runtime.py` returns nothing.
  Evidence: `MODEL_RUNTIME=ollama .venv/bin/python -c "model_runtime.extract(w2_test)"` → JSON `{"doc_type":"W-2","employee_name":"Marcus D. Whitfield","box2_fed_withheld":"9,183.44"}`. Scoped grep `grep -rnE "11434|api/generate|chat/completions|COURIER_BASE_URL" backend/ --include=*.py --exclude-dir=.venv | grep -v model_runtime.py` → CLEAN (only backend source is model_runtime/pipeline/main; venv is gitignored, ignore its lib hits). Commit `be0662d`.

- [x] **T11 — FastAPI endpoints per docs/API.md** (V/agent)
  DoD: `/intake`, `/queue`, `/documents`, `/documents/{id}`, `/documents/{id}/image`, `/documents/{id}/confirm`, `/clients`, `/stats` all return contract-shaped JSON; `state.json` persisted on every mutation.
  Verify: curl sequence — POST a testset image to `/intake` → doc reaches `status: extracted` in `/documents` → POST `/confirm` with one changed field → doc `confirmed`, field carries `corrected: true` + `original_value`, client checklist updates; kill and restart server → state intact.
  Evidence: uvicorn :8100 — `POST /intake -F file=@eval/w2_test.png` → `{"queued":["doc_001"]}`; polled `/queue` to `done:1`; `/documents/doc_001` → `status:"extracted"` all 5 W-2 fields (box2 `9,183.44`), `received_at` set; `POST /documents/doc_001/confirm` with box2→`"8,000.00"` → `box2_fed_withheld:{"value":"8,000.00","corrected":true,"original_value":"9,183.44"}`, `status:"confirmed"`, `/clients` → `received_docs:["W-2"]`; `pkill uvicorn` then restart → `/documents/doc_001` still `confirmed` with correction + client intact, `/queue` `done:1`. `/image` → `http=200 image/png`. Commit `be0662d`.

- [x] **T12 — Classification + extraction prompts** (V/agent)
  DoD: strict-JSON prompts at temperature 0; unparseable JSON → one retry → `UNRECOGNIZED`; per-type field keys match docs/API.md.
  Verify: `w2_clean_01.png` through the real pipeline → `doc_type: "W-2"` with all five W-2 field keys present.
  Evidence: intake `w2_clean_01.png` → `status:"extracted"`, `doc_type:"W-2"`, field keys `['employee_name','ssn','employer','box1_wages','box2_fed_withheld']` (all five). NOTE: field VALUES came back empty on this image — the known testset generator illegibility bug, NOT a prompt bug (same pipeline on `eval/w2_test.png` returns 6/6 correct incl. box2 `9,183.44`); DoD is type + keys, both correct. Empty values are honestly flagged `low_confidence`. Commit `be0662d`.

- [x] **T13 — UNRECOGNIZED path** (V/agent)
  DoD: non-tax documents are never force-fit; they land in review queue for manual classification, and manual classify → normal confirm flow.
  Verify: `receipt_01.png` through the pipeline → `status: unrecognized`; then POST `/confirm` with a manual `doc_type` + `client_id` succeeds.
  Evidence: intake `receipt_01.png` → `status:"unrecognized"`, `doc_type:"UNRECOGNIZED"`, `fields:{}` (not force-fit); then `POST /confirm {"client_id":"client_whitfield_m","doc_type":"1099-MISC","fields":{"payer":"Acme Corp"}}` → `status:"confirmed"`, `doc_type:"1099-MISC"`, `/clients received_docs` gained `1099-MISC`. Commit `be0662d`.

- [x] **T14 — Event log + /stats/timeline (STRETCH — only after T10-T13 green)** (V/agent)
  DoD: backend appends extraction/confirm events to `backend/events.jsonl` per docs/API.md "Event log"; `GET /stats/timeline?hours=24` aggregates buckets + totals incl. corrections_by_category and first_try_type_acc.
  Verify: process 2 docs, correct 1 field, confirm both → timeline totals show 2 docs, correct correction count, category attribution matches the corrected key.
  Evidence: T10-T13 green first. `events.jsonl` carries `extracted`/`unrecognized`/`confirmed` rows in contract shape (incl. `fields_low_confidence` per `acbe402`). Ran 3 docs / 2 confirms / 1 field correction → `GET /stats/timeline?hours=24` totals: `docs_processed:3, fields_extracted:10, fields_low_confidence:5, fields_corrected:2, correction_rate:0.2, first_try_type_acc:0.5, median_latency_s:15.73, corrections_by_category:{"money":1,"names":1,"doc_type":1}` — attribution matches corrected keys (box2_fed_withheld→money, payer→names, receipt→1099-MISC type change→doc_type). Separately: w2_clean_01 (empty fields) → `fields_low_confidence:5` in its extracted event; w2_test (valid) → `0`. Commits `be0662d` + `966ba5f`.
  ADDENDUM (contract fix + observability): live E2E found two timeline contract violations, pinned red by `backend/tests/test_timeline_contract.py` (`04d0c52`) — sparse buckets and zero-count categories omitted. Fixed: exactly `hours` zero-filled buckets oldest-first ending current hour; `corrections_by_category` always carries all four keys. Both tests green (`pytest backend/tests/test_timeline_contract.py` → 2 passed), plus `test_api_contract.py` 4 passed and `eval/test_scoring.py` 8 passed, verified in a clean worktree at `04d0c52` + this change (the shared tree's in-flight re-ask/cascade `pipeline.py` edits break the api suite's fake-adapter signature — that lane's to reconcile). Bundled: per-doc raw model I/O capture → `backend/raws/{doc_id}.json` (gitignored) referenced as `raw_ref` in the extracted event, and `p95_latency_s` in timeline totals; real-model check: w2_test → raws file with 2 calls (exact classify+extract prompts + raw responses), event `raw_ref:"raws/doc_001.json"`, timeline `buckets=24` zero-filled, `p95_latency_s:32.0`, categories all present.

## Phase 2 — Eval (owner V/agent; target ~12:30 PM)

- [x] **T20 — `eval/run_eval.py` per docs/EVAL.md** (V/agent)
  DoD: imports the backend adapter + production prompts (not copies); implements the scoring rules (money normalization, casefold strings, silent-wrong-value counter); emits summary + `eval/results.json`.
  Verify: run against any 3 testset images with labels; hand-check one scored field against labels.json.
  Evidence: `run_eval.py --model gemma4:e4b --labels labels.json --docs ./testset/ --images w2_clean_01.png,1099int_clean_01.png,receipt_01.png` → completed, `doc-type accuracy: 3/3 (100.0%)`, `field accuracy: 0/8 (0.0%)`, `silent wrong values: 0`, `median latency: 13.1s`, wrote results.json. `run_eval.py` imports `backend/pipeline.run_pipeline` (not a copy). Hand-check: w2_clean_01 `box1_wages` expected `"101775.13"` (== labels.json) vs predicted `""` → verdict `missing` (empty counts as miss, not silent-wrong — correct). Field 0/8 is the known testset illegibility bug, not a scorer bug. Commit `a7cb7d2`. (Full 26-doc runs = T21/T22, left for orchestrator.)

- [x] **T21 — Full e4b run over the 26-doc test set** (V/agent)
  DoD: `eval/results.json` committed with doc-type accuracy, field accuracy, silent-wrong count, median latency.
  Verify: `python run_eval.py --model gemma4:e4b ...` completes all 26; results.json parses; numbers transcribed nowhere they don't match.
  Evidence: `backend/.venv/bin/python eval/run_eval.py --model gemma4:e4b --labels eval/labels.json --docs eval/testset/` on the FIXED testset (commit `e376cc8` content-crop + W-2 SSN placement, regenerated in `39b1aa8`) → all 26 scored, `doc-type accuracy: 26/26 (100%)`, `field accuracy: 41/94 (43.6%)`, `silent wrong values: 23`, `median latency: 17.23s`. results.json parses (json.load, `docs_scored: 26`). Split: clean 29/47 fields, photo 12/47. Misses are genuine vision errors (e.g. `Coppell Bank` for `Copperline Bank`), not the pre-fix all-empty artifact. Commit `30a66b7`.

- [x] **T22 — e2b comparison run** (V/agent)
  DoD: same set through `gemma4:e2b`; comparison table committed (extends the kill test from n=1 to n=26).
  Verify: results file for e2b exists; silent-wrong count for each model recorded.
  Evidence (orchestrator, Sat AM): `eval/results_final_e2b.json` git-tracked (`git ls-files` confirms) and parses — summary `{"model":"gemma4:e2b","docs_scored":29,"doc_type_accuracy":1.0,"field_correct":40,"field_total":106,"field_accuracy":0.3774,"silent_wrong_values":36,"median_latency_s":13.02}`; e4b counterpart `results_final_e4b.json` records `silent_wrong_values:21` — both models' silent-wrong counts recorded. Comparison table committed in docs/WRITEUP.md + docs/OVERNIGHT.md; every transcribed number cross-checked against the json this morning (37.7% = 40/106, 62.3% = 66/106 exact).

- [ ] **T23 — Real phone-photo bucket** (V)
  DoD: ≥2 printed-then-photographed docs added to testset with labels; eval includes them.
  Verify: new files in labels.json; rerun eval covers them.
  Evidence: _none_

- [x] **T24 — REGION_PASS gated accuracy pass (Round 2)** (agent)
  DoD: per-field region crops (`backend/regions.py`, fractional boxes) + `REGION_PASS` flag in `pipeline.py` (default OFF until gated) that re-reads each empty/format-failed/name field from a padded crop with a single-field call; teeth on acceptance (format check for money/TIN, label-blacklist + len≥3 for names); `run_eval.py` gains `flag_coverage`; cross-field validators (W-2 box2<box1, money>0, label-echo) feed low_confidence. GATES vs the shipping-config baseline (`results_final_e4b.json`, PREPROCESS=1 RE_ASK=0 CASCADE=0): field accuracy +≥5, silent_wrong ≤16, median latency +≤6s.
  Verify: coordinate sanity by cropping real testset images and reading them; killer test on the 1098 lender; full gated 29-doc run.
  Evidence: **VERDICT — ships default OFF: 2/3 gates PASS massively, latency gate FAILS.** Coordinate sanity: cropped + eye-read 13 (doc_type,field) pairs across clean/photo/hand (preprocess de-warps photos to ~1700×1055 so the same fractions land on the same boxes; clean+hand renders pass through byte-identical). Killer test (1 GPU call): 1098 lender whole-image `'Coppell Bank'` → region-crop `'Copperline Bank'` (correct), crop calls 3.2–3.8s warm. Full gated run `MODEL_RUNTIME=ollama PREPROCESS=1 RE_ASK=0 CASCADE=0 REGION_PASS=1 run_eval.py --model gemma4:e4b --out results_e4b_region.json` (uncontended GPU, verified no concurrent run_eval): field **66→98/106 (+32, 62.3%→92.5%, +30.2pp) PASS**; silent_wrong **21→8 PASS**; median latency **17.7→28.2s (+10.5s) FAIL** (each doc fires 2 mandatory name re-reads + empties at ~3.5s; ≤6s is structurally unreachable with names-always on this host). K-1 recovered 16/16 previously-empty fields; lender/payer/garbled-name reads fixed. Name-field replacement audit: 33 fixes, **1 regression** (`w2_hand_01` employer `'Hollow Pine Outfitters'`→`'Hollow Pine Outfitters\n802 Timberline Rd'` — box c crop includes the address; true replacements 14 correct / 1 wrong). flag_coverage on silent-wrongs = **0/8** before AND after validators (the 8 remaining are format-valid plausible misreads — wrong-digit TINs/SSNs, plausible money — that no deterministic check catches; baseline was 0/21). `results_e4b_region.json` committed. `pipeline.py`/`run_eval.py` also carry the concurrent HAND_ENSEMBLE lane's uncommitted edits (shared tree). Commit `84cf766`.

## Phase 3 — Frontend (owner V; target ~12:30 PM)

- [ ] **T30 — Capture/Submit screen** (V)
  DoD: drag-and-drop posts files to `/intake`; queue progress polls `/queue`; paper/ink tokens per docs/design/DESIGN.md; "Processed on this Mac. Nothing is uploaded." visible.
  Verify: drop 2 testset images in a browser → both appear in `/documents` and progress shows.
  Evidence: Frontend half verified in mock mode (`frontend/`, branch `agent/vin-overnight`) — dropped 2 files onto the zone → "Queued · 2 files" list → Process → `/queue` polling rendered "0 of 2" with progress bar → "2 documents ready", and both materialized into Review (doc_007 `1099-INT`, doc_008 `UNRECOGNIZED`, each with preview image). Paper/ink tokens + "Processed on this Mac. Nothing is uploaded." present; page load fires ZERO external network requests (all `localhost` + `blob:`, Caveat font from local `assets/caveat.woff2`). Awaiting backend for full DoD (real `/intake` round-trip). LIVE-STACK ADDENDUM (orchestrator ~01:40): real /intake verified via curl multipart (2 files, repeated `file` key) → processed → rendered in Review with images; box stays unchecked only because the literal browser drag-and-drop gesture on the real stack hasn't been performed — covered by T40's morning Wi-Fi-off run.

- [x] **T31 — Bin Review & Correction screen** (V)
  DoD: source image beside extracted fields; editing a field and confirming POSTs `/confirm`; corrected value renders red-strike original + ink-blue correction; survives reload.
  Verify: correct one field in the browser → reload → correction still displayed; `/stats` correction count incremented.
  Evidence (full DoD, live stack, orchestrator ~01:45): real browser against running backend on :8100 — selected Ruth Okafor, edited box2_fed_withheld on doc_001 (real e4b extraction of w2_test.png), Confirm → GET /documents/doc_001 showed `{"value":"9,999.99","corrected":true,"original_value":"9,183.44"}`, status confirmed; /stats went to fields_corrected:1, correction_rate:0.2; state persisted server-side (state.json). Mock-mode render spec evidence below stands.
  Prior evidence: Frontend half verified in mock mode (`frontend/`, branch `agent/vin-overnight`) — source image renders beside editable fields; corrected Marcus Whitfield W-2 Box 2 `70,110.00`→`9,183.44` in the browser, rendered original struck in red pen (computed `rgb(192,57,43)` + `line-through`) beside corrected value in ink blue (`rgb(47,95,208)`, weight 700) with a Caveat "corrected" note; the correction persists across reload (localStorage in mock; real backend `state.json` for full DoD) and `/stats` corrected-count went 1→2. UNRECOGNIZED receipt shows the manual `doc_type` + client pickers, empty confirm is blocked ("Pick a document type first"), and classifying it as K-1 for Chen flowed to the checklist. Awaiting backend for full DoD (real `/confirm` + server-side reload persistence).

- [x] **T32 — Checklist Dashboard** (V)
  DoD: clients from `/clients`; confirming a doc checks its checklist item with the ink animation; missing items obvious; stats line shows fields extracted / corrected.
  Verify: confirm a W-2 for a client expecting one → item inks in; client missing a K-1 shows it missing.
  Evidence (full DoD, live stack, orchestrator ~01:50): real browser, live /clients — confirmed W-2 for Ruth Okafor (expected [W-2]) → row inked "all in ✓" with "Received Jul 18 · 1 correction"; Chen Partnership showed K-1 + 1098 MISSING in highlighter; Marcus Whitfield showed W-2 + 1099-INT MISSING; stats line rendered live 5 extracted / 1 corrected / 20.0%. Screenshot in transcript.
  Prior evidence: Frontend half verified in mock mode (`frontend/`, branch `agent/vin-overnight`) — three journey clients render with correct gaps: Ruth Okafor 2/3 then 3/3 ("all in ✓") after confirming her 1099-INT, with the ink-in animation classes (`row-settle` + `ink-draw` check path) applied to ONLY the newly-confirmed row; Marcus Whitfield shows 1099-INT MISSING in highlighter (#ffd24a) with a Request link; Chen Partnership shows K-1 + 1098 MISSING. Stats line renders from `/stats` (fields extracted / corrected / correction rate, e.g. 26 / 2 / 7.7%). Awaiting backend for full DoD (live `/clients` + `/stats`).

- [x] **T33 — "Stats for Nerds" screen (STRETCH — only after T30-T32 green)** (V/agent)
  Evidence (full DoD, live stack, orchestrator Sat ~8:55 AM): real browser on :8100 (server sha `401b8c8`, seed state) — Nerd stats view renders LIVE `/stats/timeline?hours=24` data: tiles 2 docs / 100% first-try / 20.0% correction rate / 16.81s median with p95 23.33s, exactly matching a parallel curl of the endpoint; 24 hour bars with "now ←" + axis labels; extraction block 5 fields / 0 flagged / 1 corrected (20.0%); corrections-by-field money=1, others 0; taglines present ("The dashboard shows the last 24 hours. Nothing leaves this Mac." — the deliberate IMPROVEMENTS #7 honest-copy variant of the mockup line — + Caveat "the red-pen rate is the number to watch"). Zero console errors. Mock-mode half verified previously (see prior evidence below).
  DoD: fourth view rendering `GET /stats/timeline?hours=24` per mockup screen 3 (docs/design/tax-intake-mockup.html): headline tiles (docs processed, first-try classification %, correction rate, median latency), docs-per-hour bars, corrections-by-category list, the "Past 24 hours only... Nothing leaves this Mac" line, "the red-pen rate is the number to watch" tagline.
  Verify: with seeded events, all tiles render real numbers; mock mode works without backend.
  Evidence: Frontend half verified in mock mode (`frontend/`, branch `agent/vin-overnight`) — fourth "Nerd stats" view renders `mock/timeline.json` (exact `GET /stats/timeline?hours=24` shape): tiles 31 docs / 94% first-try / 4.2% correction rate / 19.2s median; 24 CSS bars (last 3 ink-blue, "now ←" + axis labels); extraction block 214 / 17 · 7.9% flagged (highlighter) / corrected in red; corrections-by-category 4/2/2/1; both required lines present ("Past 24 hours only — stats reset as they age out. Nothing leaves this Mac." + Caveat "the red-pen rate is the number to watch"). Mock overlays live deltas the way the backend will recompute from events.jsonl: demo-time corrections ticked corrected 9→10 (rate tile 4.2%→4.7%), money 4→5, doc_type reclass 1→2, flagged count correctly did NOT shrink. Zero external requests, console clean. Awaiting backend T14 (`events.jsonl` + real `/stats/timeline`) for full DoD.

## Phase 4 — Integration + runtime (both; target ~1:00 PM = FREEZE)

- [ ] **T40 — E2E on the demo Mac, fully local** (V/agent)
  DoD: folder-drop → classify → bin → review/correct → checklist, all on the M4 with default env (no Tailscale dependency).
  Verify: run the full flow once **with Wi-Fi off** — this is also the demo's on-device proof.
  Evidence: _none_

- [x] **T41 — Courier OS verification (env flip)** (V)
  DoD: install + auth; kill test via `MODEL_RUNTIME=courier`; result recorded in PRD §8 EITHER WAY. Only a pass permits naming Courier in writeup/demo.
  Verify: `run_test.py` equivalent through the courier adapter path returns 6/6 on the W-2, or the failure is documented.
  Evidence (orchestrator, Sat ~9:10 AM, Vin authorized start): **FAIL — documented in PRD §8, Courier not named anywhere public.** `scripts/courier_bakeoff.sh` preflight PASS (`ollama ps` empty, `/v1/models` lists `gemma4:e4b` + `gemma4:e2b`, no id overrides needed); e2b image sanity FAIL — 3× 60s adapter timeouts, then a direct call with `timeout=240` → `[240.0s] FAILED: TimeoutError` (system 70% memory free at request time — resources were not the constraint this run; image inference simply never returned). Kill test + subset not reached (gated on sanity). WRITEUP runtime line finalized to Ollama-only; DEMO-SCRIPT 0:35 line finalized. Install/auth half was completed Fri overnight (self-hosted Personal edition, local key in backend/.env).

- [x] **T42 — Demo seed data** (agent)
  DoD: `state.json` with 3 clients matching docs/USER-JOURNEY.md — Ruth Okafor (complete after one confirm), Marcus Whitfield (missing 1099-INT), Chen partnership (missing K-1 + 1098).
  Verify: dashboard renders the three rows with exactly those gaps.
  Evidence: `backend/state.demo.json` (5 docs, images copied from `eval/testset/` + `eval/w2_test.png`, real labels.json field values). `./scripts/demo_state.sh seed` against the RUNNING :8100 server → `GET /clients` → `client_ruth_okafor` expected `["W-2","1099-INT","1098"]` received `["1099-INT","1098"]` (doc_003 W-2 sits `status:"extracted"` — the confirm moment); `client_marcus_whitfield` expected `["W-2","1099-INT"]` received `["W-2"]` (1099-INT has no document at all); `client_chen_partnership` expected `["K-1","1098"]` received `[]` (zero docs). `GET /documents/doc_003` → W-2 extracted, all 5 fields filled, awaiting review. `GET /documents/doc_005` → `UNRECOGNIZED`, `fields:{}`, `client_id:null` (receipt awaiting manual classification). `doc_004` (Marcus's confirmed W-2, `eval/w2_test.png`) carries the correction already recorded: `box2_fed_withheld:{"value":"9,183.44","corrected":true,"original_value":"70,110.00"}` — the exact kill-test number from docs/API.md and PRD. Image serving: `doc_001`..`doc_005` all `200 image/png`. `GET /queue` → `{"pending":0,"processing":null,"done":5}` (nothing stuck). Seed left loaded per instructions. Commit `4712c53`.

- [x] **T43 — Pre-processed fallback session** (agent)
  DoD: a fully-processed backup `state.json` + one-command restore, for use if live processing stalls on stage.
  Verify: restore command swaps state and dashboard renders instantly.
  Evidence: `backend/state.fallback.json` (adds doc_006 Marcus 1099-INT confirmed + doc_007 Chen K-1 confirmed, plus doc_003 flips to `confirmed`) + `scripts/demo_state.sh` (portable bash 3.2, no assoc arrays — stages images from `eval/` into `backend/uploads/` since `uploads/` is gitignored, copies the chosen state file to `backend/state.json`, then kill+restarts uvicorn on :8100 because `main.py` only loads state at the FastAPI startup event, no hot-reload path). `./scripts/demo_state.sh fallback` against the running server → `GET /clients`: Ruth `received_docs:["1099-INT","1098","W-2"]` (3/3 complete), Marcus `["W-2","1099-INT"]` (2/2 complete), Chen `["K-1"]` (1/2, mostly complete — 1098 still open, matches DoD "mostly complete" not staged as suspiciously perfect). `doc_003` status flips `extracted`→`confirmed`. `doc_006`/`doc_007` images `200 image/png`. Then `./scripts/demo_state.sh seed` run again → dashboard flips back to the T42 gaps (verified above) and left loaded as the final state. Commit `4712c53`.

- [ ] **T44 — FREEZE at 1:00 PM** (all)
  DoD: no feature code after 1:00 PM; only demo prep, writeup, and fixes for demo-blocking bugs.
  Evidence: _none_

## Phase 5 — Demo + submission (hard deadlines)

- [ ] **T50 — Three stopwatch dry-runs** (V/agent; by 2:00 PM)
  DoD: three timed runs of the docs/USER-JOURNEY.md demo script, each ≤ 3:00; live path and fallback path each rehearsed at least once.
  Verify: times written here.
  Evidence: _none_

- [ ] **T51 — Kaggle Writeup submitted** (V; **by 3:00 PM — no writeup = ineligible**)
  DoD: product story, model stack (verified runtime only — see T41), GitHub link, eval numbers that match `eval/results.json` exactly.
  Verify: submission confirmation visible; a teammate cross-checks numbers against results.json.
  Evidence: _none_

- [ ] **T52 — Repo final sweep** (agent; by 2:45 PM)
  DoD: README numbers match results.json; no secrets (`grep -ri "api[_-]key\|secret\|token" --exclude-dir=.git` clean or false-positives only); repo confirmed public; fresh-clone run instructions actually work.
  Verify: run the greps + `gh repo view --json isPrivate`; fresh clone in /tmp follows README successfully.
  Evidence: _none_

- [ ] **T53 — Demo logistics** (V/agent; by 2:55 PM)
  DoD: model warmed (one inference completed), demo docs staged, backend + frontend running, screen/adapter tested, fallback restore command in a ready terminal.
  Verify: one warm inference logged < 5 min before demo slot.
  Evidence: _none_

## Phase 6 — Vin's morning product gaps (Sat AM; triage against the 1 PM freeze — small ones may ship today, rest are honest post-demo backlog)

- [x] **T60 — Dashboard content pass** (agent; SMALL, demo-visible)
  DoD: editorial copy removed — "measured on real documents" (dashboard stats line) and "Processed on this Mac. Nothing is uploaded." (capture + review) are gone per founder direction; replaced with operational info that earns the pixels: docs-awaiting-review count, last-intake timestamp, per-client progress fractions. PRD §10 note updated so the copy change is deliberate, not drift.
  Verify: browser screenshots of all views show no flagged phrases; new operational elements render from live data.
  Evidence: lane commit `58677d8`, merged `25e3050`. Agent: `grep -rn` both flagged phrases across frontend/ → zero matches (retained non-flagged nerd line "Nothing leaves this Mac" intact); op elements computed from live `/documents`+`/clients` (node-executed helpers against a live instance). Orchestrator browser pass on :8100 (real screenshots, merged code): capture foot shows "2 awaiting review · last intake 1:47 AM" where the old copy sat; review header carries the same op-note; dashboard stats line shows "last intake 1:47 AM" (no "measured on real documents"); per-client fraction chips render (Ruth 3/3 "all in ✓", Chen 0/2). PRD §10 note committed. Zero console errors.

- [x] **T61 — Identity confirmation + multi-page support in Review** (agent; MEDIUM)
  DoD: confirm flow requires an explicit client-identity confirmation (not a silently-defaulted dropdown — "This document belongs to [client]" is an affirmative act); Document gains optional `page_number`; docs without an extractable name (continuation pages) can be assigned client + page number manually. Rationale recorded: misassignment = confidentiality incident for a tax firm — this is the human gate applied to identity.
  Verify: confirming without an explicit client selection is blocked; a page-numbered doc round-trips through confirm and renders its page label.
  Evidence: lane commit `83fff28`, merged `26f9f6f` (+4 tests, suite green). Design note: existing contract test `test_state_machine_edges` requires confirm with `client_id:null` → 200, so the affirmative act is enforced in the FRONTEND (per DoD guidance; backend `page_number` purely additive, invalid values 400 without mutation). Orchestrator click-through in real browser on merged :8100: doc_003 (Ruth W-2) opened with UNCHECKED "This document belongs to Ruth Okafor", Confirm rendered disabled with red "Confirm the client identity first"; checking the affirmation enabled Confirm; set page 1; confirmed → server shows `status:confirmed, page_number:1, client_id:client_ruth_okafor`, Ruth `received_docs` gained W-2 (3/3, row inked). Rationale recorded in docs/USER-JOURNEY.md + code comment (misassignment = confidentiality incident).

- [x] **T62 — CSV export** (agent; SMALL, integration story)
  DoD: `GET /clients/{id}/export.csv` — confirmed documents with extracted+corrected field values, one row per field or per doc (pick and document); UI download link on the client card. Writeup line: "integrates with anything that imports CSV today; QuickBooks Online / TaxDome APIs are the named roadmap" — no deeper integration claims without verification.
  Verify: export a seeded client, open the CSV, values match the confirmed state.
  Evidence: lane commit `e9b86f0`, merged `20fae1c` (+6 tests, suite green). One row per FIELD (documented in docs/API.md "CSV export"): `client_id,client_name,doc_id,doc_type,received_at,field_key,field_label,value,corrected,original_value,low_confidence`. Agent live-curl on seeded state — Marcus's corrected row quoted: `...,box2_fed_withheld,Fed. tax withheld (Box 2),"9,183.44",true,"70,110.00",false` (corrected value + original preserved, comma-money quoted); headers `text/csv` + attachment filename; confirmed-only filter proven (extracted doc_003 absent); empty client → header-only; unknown → 404. Orchestrator on merged :8100: endpoint 200 text/csv via curl; "Export CSV ↓" link renders on client cards in real browser. Writeup line added verbatim to docs/WRITEUP.md. NOTE (post-research, ~10:15 AM — DoD kept intact per rule 4, line amended in WRITEUP only): integration research verified TaxDome has NO public API (Zapier-only, no document actions), so naming it was puncturable; roadmap line now names Karbon + Canopy (verified public APIs) + QBO Attachable, and states tax-prep engines are closed. Q&A crib gained the "why no QuickBooks" answer.

- [x] **T63 — CRUD gap audit + delete path** (agent; MEDIUM)
  DoD: written audit of missing essential CRUD across frontend+backend (known gaps: no document delete for erroneous ingests, no client edit/delete, no doc→client reassignment after confirm, no expected_docs checklist editing, no un-confirm/re-open); then implement at minimum DELETE /documents/{id} (removes doc + un-checks its checklist item + logs a deletion event) with a UI affordance and confirmation dialog; tests pin the delete semantics.
  Verify: ingest a junk doc, delete it from the UI, checklist and stats reflect removal, state survives restart, test green.
  Evidence: test-first loop honored — Codex red tests `2cb5df6` (6 tests, all failing 405 for the right reason) → implementation `03a566a` merged `b0f6c1c`, all 6 green without touching the test file (byte-identical). Agent live-curl: DELETE doc_004 → `{"deleted":"doc_004"}`, GET → 404, Marcus `received_docs ['W-2']→[]` (count-aware: two same-type docs pinned by test — deleting one keeps the item), `{"type":"deleted","doc_id":"doc_004"}` in events.jsonl, restart → still gone, stats moved `fields_extracted 16→11`/`corrected 1→0`. Orchestrator UI click-through in real browser on merged :8100: selected the seeded junk receipt (doc_005 UNRECOGNIZED), clicked "Discard this document" → dialog text ("Discard this unrecognized document (no client yet)? … can't be undone.") → doc_005 GET → 404, queue done 5→4, doc left the Review list. docs/CRUD-AUDIT.md committed (gaps: client edit/delete, reassignment after confirm, expected_docs editing, un-confirm — honest post-demo backlog).

- [x] **T64 — Richer mock + demo data** (Sonnet agent; SMALL)
  DoD: additional clients in frontend/mock fixtures AND an alternate seed (state.demo-big.json): 6-8 clients, several with long realistic checklists (8-12 expected docs — W-2s, multiple 1099 types, K-1, 1098, charitable receipts), varied completion states, at least one nearly-empty and one nearly-complete.
  Verify: dashboard renders the big seed legibly (no layout breakage with long checklists); mock mode unaffected.
  Evidence: lane commit `b35210f` (Sonnet), merged `4628dca`. `state.demo-big.json`: 8 clients / 23 docs, canonical 3 journey clients preserved with exact canonical states; nearly-empty Dmitri Volkov 0/4; nearly-complete Priya Nandakumar 8/9 (missing one 1099-MISC); Whitmore & Cole LLP 10-item checklist. Multi-instance types disambiguated ("W-2 — Solstice Health Partners") so exact-string checklist matching stays correct; received items only reference images that exist in eval/. `scripts/demo_state.sh big` verb added — orchestrator RAN it against :8100: `/clients` → 8 clients with the expected fractions, then real-browser scroll-through: stats 85 fields/1.2%, Priya 8/9 and Whitmore 5/10 render cleanly, cards grow naturally, MISSING chips + per-card Export CSV intact. Mock fixtures extended consistently + mock stats BASELINE recomputed (26→114 fields) so mock nerd-stats don't inflate. Restored `seed` after verification. NOTE for demo: mock-mode browser cache is keyed `keepbook_mock_v4` — hit `?reset=1` if mock mode was used earlier in the same browser.

- [ ] **T66 — Check register + bank statement ingestion** (agent; CUT-ELIGIBLE — added 9 AM, default = post-demo backlog unless the human track finishes early)
  DoD: add `check register` and `bank statement` as classify-only types (T65 pattern: classify + client-assign + confirm, zero extraction — transaction-level line-item extraction is a different product surface (reconciliation) and is explicitly post-demo); both satisfy checklist items; junk discipline unchanged. Note: check registers are often HANDWRITTEN and multi-page — pairs with T61 page_number and the handwritten-subset plan (register would join W-9/1099-MISC as a hand-filled eval candidate).
  Verify: same as T65 — classify/assign/confirm/checklist round-trip with fake adapter; sample images optional (eval bucket UNVERIFIED label if unrun).
  Evidence: _none_

- [ ] **T67 — Organizer-derived expected checklists** (agent; CUT-ELIGIBLE — added 9 AM, default = post-demo backlog)
  DoD: expected_docs gets a provenance story instead of a hand-typed list. Three layers: (1) firm's generic organizer = a TEMPLATE (`expected_docs` starter set, one shared list in config); (2) per-client pruning/additions editable in the UI (expected_docs editing is already a named gap in docs/CRUD-AUDIT.md); (3) year-2 flywheel: seed next season's expected_docs from THIS season's confirmed docs per client — the product replaces the organizer's "what did you send last year" function with its own confirmed history. Writeup line if it ships: "after one season, the checklist writes itself from last year's confirmed documents."
  Verify: template applies to a new client; per-client edit round-trips; a derived checklist matches the client's prior confirmed doc types.
  Evidence: _none_

- [ ] **T68 — Progressive Web App packaging** (agent; POST-DEMO — added Sat AM per founder)
  DoD: manifest.json (name, theme paper/ink colors, icons from logo-mark.svg rendered to 192/512 PNG), installability from localhost Chrome ("Install KeepBook" → dock/Launchpad icon launches the app window); optional service worker for offline shell (aligns with the on-device story — the whole app already runs offline once served). Note: install requires localhost or HTTPS — localhost qualifies, so no cert work needed for the single-machine product.
  Verify: Chrome shows the install affordance; installed icon launches to the dashboard.
  Evidence: _none_

- [x] **T65 — Classify-only document types** (agent; MEDIUM, eval-gated)
  DoD: extend the type enum with classify-only types (`extract: false` in schema): 1099-DIV/-B/-R/-G, 1098-T/-E, 1095-A, property tax statement, charitable receipt, brokerage statement, W-9, engagement letter. These get classified + client-assigned + human-confirmed, zero field extraction — so the silent-wrong failure class cannot exist for them; they still satisfy checklist items. Risk register (recorded): larger enum = more force-fit surface (mitigation: UNRECOGNIZED discipline unchanged + mandatory confirm); new types eval-unverified (mitigation: small classify-only eval bucket, labeled UNVERIFIED until run).
  Verify: a classify-only doc classifies, assigns, confirms, and checks its checklist item; junk still lands UNRECOGNIZED; eval bucket run or explicitly deferred with the UNVERIFIED label.
  Evidence: lane commit `051f708`, merged `f37b314` (+6 tests incl. exactly-one-adapter-call assert and junk→UNRECOGNIZED path; suite 54 green). 12 classify-only types in `pipeline.CLASSIFY_ONLY_TYPES`, extraction short-circuited (1 model call), Review renders clean empty-state + the types in the manual-classify dropdown; risk register recorded in PRD §4. **Eval bucket RUN by orchestrator (upgrades the lane's UNVERIFIED label): 5/5 doc-type** — `w9_01`/`charitable_receipt_01`/`brokerage_stmt_01` all classified exactly AND junk negatives `receipt_01`/`letter_01` still UNRECOGNIZED with the 18-type enum (force-fit risk did not materialize); 0 silent wrongs, 10.3s median (single-call). `eval/results_classify_only.json` committed; `eval/CLASSIFY_ONLY.md` flipped to VERIFIED with the honest caveat: n=3 samples cover 3 of 12 new types — the other 9 ship classification-untested behind the same mandatory-confirm gate.
