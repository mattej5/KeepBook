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

- [ ] **T10 — `backend/model_runtime.py` adapter** (V/agent)
  DoD: `extract(image_b64, prompt) -> str` implementing both shapes in docs/API.md, runtime/env-var selected; no other backend file contains a model URL.
  Verify: `MODEL_RUNTIME=ollama python -c "..."` returns non-empty model output for `eval/w2_test.png`; `grep -rn "11434\|api/generate\|chat/completions" backend/ --include="*.py" | grep -v model_runtime.py` returns nothing.
  Evidence: _none_

- [ ] **T11 — FastAPI endpoints per docs/API.md** (V/agent)
  DoD: `/intake`, `/queue`, `/documents`, `/documents/{id}`, `/documents/{id}/image`, `/documents/{id}/confirm`, `/clients`, `/stats` all return contract-shaped JSON; `state.json` persisted on every mutation.
  Verify: curl sequence — POST a testset image to `/intake` → doc reaches `status: extracted` in `/documents` → POST `/confirm` with one changed field → doc `confirmed`, field carries `corrected: true` + `original_value`, client checklist updates; kill and restart server → state intact.
  Evidence: _none_

- [ ] **T12 — Classification + extraction prompts** (V/agent)
  DoD: strict-JSON prompts at temperature 0; unparseable JSON → one retry → `UNRECOGNIZED`; per-type field keys match docs/API.md.
  Verify: `w2_clean_01.png` through the real pipeline → `doc_type: "W-2"` with all five W-2 field keys present.
  Evidence: _none_

- [ ] **T13 — UNRECOGNIZED path** (V/agent)
  DoD: non-tax documents are never force-fit; they land in review queue for manual classification, and manual classify → normal confirm flow.
  Verify: `receipt_01.png` through the pipeline → `status: unrecognized`; then POST `/confirm` with a manual `doc_type` + `client_id` succeeds.
  Evidence: _none_

## Phase 2 — Eval (owner V/agent; target ~12:30 PM)

- [ ] **T20 — `eval/run_eval.py` per docs/EVAL.md** (V/agent)
  DoD: imports the backend adapter + production prompts (not copies); implements the scoring rules (money normalization, casefold strings, silent-wrong-value counter); emits summary + `eval/results.json`.
  Verify: run against any 3 testset images with labels; hand-check one scored field against labels.json.
  Evidence: _none_

- [ ] **T21 — Full e4b run over the 26-doc test set** (V/agent)
  DoD: `eval/results.json` committed with doc-type accuracy, field accuracy, silent-wrong count, median latency.
  Verify: `python run_eval.py --model gemma4:e4b ...` completes all 26; results.json parses; numbers transcribed nowhere they don't match.
  Evidence: _none_

- [ ] **T22 — e2b comparison run** (V/agent)
  DoD: same set through `gemma4:e2b`; comparison table committed (extends the kill test from n=1 to n=26).
  Verify: results file for e2b exists; silent-wrong count for each model recorded.
  Evidence: _none_

- [ ] **T23 — Real phone-photo bucket** (V)
  DoD: ≥2 printed-then-photographed docs added to testset with labels; eval includes them.
  Verify: new files in labels.json; rerun eval covers them.
  Evidence: _none_

## Phase 3 — Frontend (owner V; target ~12:30 PM)

- [ ] **T30 — Capture/Submit screen** (V)
  DoD: drag-and-drop posts files to `/intake`; queue progress polls `/queue`; paper/ink tokens per docs/design/DESIGN.md; "Processed on this Mac. Nothing is uploaded." visible.
  Verify: drop 2 testset images in a browser → both appear in `/documents` and progress shows.
  Evidence: _none_

- [ ] **T31 — Bin Review & Correction screen** (V)
  DoD: source image beside extracted fields; editing a field and confirming POSTs `/confirm`; corrected value renders red-strike original + ink-blue correction; survives reload.
  Verify: correct one field in the browser → reload → correction still displayed; `/stats` correction count incremented.
  Evidence: _none_

- [ ] **T32 — Checklist Dashboard** (V)
  DoD: clients from `/clients`; confirming a doc checks its checklist item with the ink animation; missing items obvious; stats line shows fields extracted / corrected.
  Verify: confirm a W-2 for a client expecting one → item inks in; client missing a K-1 shows it missing.
  Evidence: _none_

## Phase 4 — Integration + runtime (both; target ~1:00 PM = FREEZE)

- [ ] **T40 — E2E on the demo Mac, fully local** (V/agent)
  DoD: folder-drop → classify → bin → review/correct → checklist, all on the M4 with default env (no Tailscale dependency).
  Verify: run the full flow once **with Wi-Fi off** — this is also the demo's on-device proof.
  Evidence: _none_

- [ ] **T41 — Courier OS verification (env flip)** (V)
  DoD: install + auth; kill test via `MODEL_RUNTIME=courier`; result recorded in PRD §8 EITHER WAY. Only a pass permits naming Courier in writeup/demo.
  Verify: `run_test.py` equivalent through the courier adapter path returns 6/6 on the W-2, or the failure is documented.
  Evidence: _none_

- [ ] **T42 — Demo seed data** (agent)
  DoD: `state.json` with 3 clients matching docs/USER-JOURNEY.md — Ruth Okafor (complete after one confirm), Marcus Whitfield (missing 1099-INT), Chen partnership (missing K-1 + 1098).
  Verify: dashboard renders the three rows with exactly those gaps.
  Evidence: _none_

- [ ] **T43 — Pre-processed fallback session** (agent)
  DoD: a fully-processed backup `state.json` + one-command restore, for use if live processing stalls on stage.
  Verify: restore command swaps state and dashboard renders instantly.
  Evidence: _none_

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
