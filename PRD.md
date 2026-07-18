# PRD: On-Device Tax Document Sorter

**Event:** Build with Gemma: JustBuild (Pattern office) — Track: On-Device AI with Gemma 4
**Demo machine:** Vin's M4 Pro Mac, 24GB unified memory
**Status:** Draft, in progress. Written under fable-like discipline — claims are labeled VERIFIED / INFERRED / ASSUMED, and open items are called out in their own sections rather than smoothed over.

---

## 1. Abstract

A local-only document sorter for small CPA and bookkeeping firms. Point a phone camera or drop a folder of scanned tax documents at it. The tool classifies each document by type (W-2, 1099, K-1, mortgage statement, and so on), extracts the key fields, groups documents into per-client bins, and maintains a per-client checklist of what is still missing. A human reviews and corrects every extraction before anything is trusted. Every byte stays on the machine it runs on — no client Social Security number, wage figure, or tax record ever leaves the laptop.

It must run locally because the users cannot legally or safely put client tax data into a cloud AI tool. A firm handling a stranger's SSN and tax return needs a signed data processing agreement with any processor that touches that data; pasting it into whatever chat tool is open in another tab creates uncontrolled liability. Local inference removes the processor entirely.

The most credible evidence we have: we ran the same W-2 field extraction against Gemma 4's two smallest on-device sizes. The smaller model (`e2b`) read five of six fields correctly but silently returned the wrong number for federal tax withheld — a clean, confident, wrong value. The larger model (`e4b`) got all six right. This result (VERIFIED, reproduced three times) is why we chose `e4b` and why mandatory human review is a core feature, not a nicety.

---

## 2. The Problem

Every tax season, a bookkeeper's actual job stops being accounting and becomes chasing paper. A client says they mailed the K-1. They didn't. Someone retypes a W-2's numbers into a spreadsheet by hand at 9pm, because the alternative is uploading a stranger's Social Security number to whatever AI tool happens to be open in another tab. Nobody signed off on that. Nobody's firm has a data processing agreement with it. It just happens, quietly, because the paperwork has to move and there's no time to ask permission.

That is the actual problem. Not that accounting is hard, but that the one part of the job that is pure repetitive classification has no safe, fast way to be automated, because every existing AI-in-the-loop tool means someone's financial identity leaves the building.

We tested this before building around it. We ran the same extraction against Gemma 4's two on-device sizes on a synthetic W-2. The smaller model read every field correctly except one: it silently swapped in the wrong number for federal tax withheld, confidently, in clean JSON, indistinguishable from a right answer unless you already knew the real one. We moved up a model size, reran it, and it got every field right.

The product is small on purpose: point a camera at a stack of documents, watch them sort themselves by type, watch a per-client checklist fill in as things arrive, and catch the rare wrong read before it is ever logged, without a single byte of a client's tax return leaving the laptop it is running on.

---

## 3. Users & Value

**Who:** Small CPA firms, solo bookkeepers, and independent tax preparers — the people who personally handle intake for a book of clients during filing season.

**Why cloud AI cannot serve them today (compliance and liability, not just "privacy is nice"):**
- A firm that processes client SSNs, wage records, and tax returns is handling data that carries real legal exposure. Sending that data to a third-party AI processor generally requires a signed data processing agreement with that processor. Most consumer AI tools do not offer one that a small firm can rely on, and no such agreement exists for "the chat tab someone happened to have open."
- The failure mode is not hypothetical. It is a bookkeeper at 9pm choosing between retyping numbers by hand and pasting a client's identity into an unvetted tool. Local inference removes the third party from the equation entirely — there is no processor to have an agreement with, because the data never leaves the device.

**Frequency and pain:** Intake and document-chasing is the dominant repetitive task of filing season — dozens of clients, each owing a shifting set of documents, arriving by email, photo, and paper over weeks. The work is high-volume, low-judgment classification punctuated by the anxiety of a missing form discovered late. This is exactly the shape of task that should be automatable and currently is not, for the compliance reasons above.

---

## 4. Document Types

**Core set — what the eval harness and demo actually target tonight:**
- W-2 (Wage and Tax Statement)
- 1099-NEC (Nonemployee Compensation)
- 1099-INT (Interest Income)
- 1099-MISC (Miscellaneous Income)
- Schedule K-1 (Partnership / S-Corp / Trust Income)
- 1098 (Mortgage Interest Statement)

**Extended set — same document family/structure, not in tonight's tested eval set, reasonable to expect similar behavior but UNVERIFIED:**
1099-DIV (Dividends), 1099-B (Broker/Investment Transactions), 1099-R (Retirement Distributions), 1099-G (Government Payments), 1098-T (Tuition Statement), 1098-E (Student Loan Interest).

**Classify-only set (T65, `extract: false`):** 1099-DIV/-B/-R/-G, 1098-T/-E, 1095-A, property tax statement, charitable receipt, brokerage statement, W-9, engagement letter. These are classified + client-assigned + human-confirmed with **zero field extraction** — one model call, not two, so the silent-wrong failure class cannot exist for them. They still satisfy checklist items once confirmed (matching is by doc_type string). Risk register (recorded): larger enum = more force-fit surface (mitigation: UNRECOGNIZED discipline unchanged + mandatory confirm); new types eval-unverified (mitigation: small classify-only eval bucket, labeled UNVERIFIED until run — see `eval/CLASSIFY_ONLY.md`).

**Explicit design rule:** a document that doesn't match a recognized type is classified as **"Unrecognized — needs manual classification,"** never force-fit into the nearest known category. Confidently mislabeling an unfamiliar document is worse than honestly admitting the system doesn't know what it's looking at — this is the same lesson as the `box2_fed_withheld` failure in §9: a wrong answer that looks right is the dangerous case, not a system that says "I'm not sure."

---

## 5. Future Directions / Nice-to-Haves

None of the following are in scope for tomorrow's demo. Listed here to show product direction for the writeup and pitch, and so nothing gets confused as a commitment under time pressure.

| Idea | Honest scope note |
|---|---|
| Automated P&L drafts | Reframe needed: a P&L is normally an **output** the firm produces from a client's transaction history, not a document a client hands in. Future direction is generating a draft P&L from already-ingested bank/invoice data, reviewed by the accountant — a different technical shape (generation, not classification) than tonight's build. |
| Automated balance sheets | Same reframe as P&L, bigger lift — requires tracking assets/liabilities/equity over time from ingested data, not a single-document extraction. Later-stage feature. |
| Client-facing voice query agent (client calls in, voice agent answers by querying the system) | Flag clearly: this is client-facing and real-time, a materially higher bar than tonight's internal bookkeeper-review tool, where a human catches mistakes before anything is trusted. A wrong answer given directly to a client isn't caught by anyone first. Deserves its own accuracy bar and scoping, not a bolt-on. |
| Invoice tracking with proactive overdue/near-overdue nudges (accountant-to-client and client-to-other-entity) | Real and valuable, but needs a background/scheduled monitoring loop — a different architecture pattern than tonight's on-demand request-response system. Genuine v2 feature. |
| Data auditing | Overlaps with the reconciliation/anomaly-flagging ideas already discussed earlier tonight (matching bank statements against internal books). Natural extension of the same classification pipeline once transaction data is flowing regularly, not one-time document intake. |
| Compliance / deadline tracking | Same shape as the deadline-tracking idea from earlier tonight (quarterly estimated taxes, sales-tax filings, 1099 issuance dates). Tool-call + calendar pattern, not reasoning-heavy — cheap to add later, not built tonight. |
| Anomaly detection | Same as the anomaly/fraud-smell flagging and financial-statement variance-flagging ideas discussed earlier tonight. Natural extension once real client transaction history is being ingested over time. |
| Tax software / filing integration | **Flag explicitly, do not soften this one:** actually filing or preparing a return is licensed tax-preparer territory with real regulatory weight (IRS e-file authorization, professional liability). This tool stays firmly in "organizes and verifies intake data for a human preparer" territory — it never claims or implies it files, advises on, or prepares a return. Same inform-don't-advise boundary already applied elsewhere in this product's design. |
| Additional proactive ideas worth naming | Auto-drafted "still waiting on" nudges per client (human sends, ties directly to the checklist already built); a distinct "never seen this document type before" flag, separate from a normal missing-item flag; duplicate-submission detection (same document uploaded twice, e.g. emailed and photographed); new-client onboarding checklist (engagement letter, W-9) tracking. |
| Distribution path | Pilot stage: white-glove install for the first firms (deliberate — high-touch onboarding doubles as user research, and compliance-sensitive buyers want to be told exactly what's on the machine). Product stage: signed + notarized Mac app (Tauri/Electron shell around the existing local backend + web UI) with the inference runtime **embedded** as a subprocess, not required as a prerequisite. First-run wizard: hardware check (Apple Silicon, ≥16GB), model download with progress, then the app runs the synthetic kill-test W-2 on itself and shows the result before touching a client document — a self-verifying install. Windows port matters commercially (bookkeeping is QuickBooks/Windows country); the §8 runtime adapter keeps that door open (Ollama is cross-platform, Courier is Mac-only). |

---

## 6. Scope

Hard demo deadline: **Saturday 3:00 PM** (Kaggle Writeup + repo), demo 3:00-4:30 PM.

| Item | Tier | Notes |
|---|---|---|
| Document intake (photo or folder of images) | **Core** | Folder-drop is the guaranteed path; phone capture is the stretch version of intake. |
| Classification (doc type) + field extraction | **Core** | Gemma 4 on-device via local API. |
| Bin grouping (documents grouped per client) | **Core** | Binning logic lives in the backend. |
| Human review + correction UI | **Core** | Load-bearing given the `e2b` failure below. Reviewer confirms or corrects every extracted field. |
| Per-client missing-document checklist | **Core** | The hero of the product — visible answer to "what is this client still missing." |
| Eval harness with a real scored test set | **Core** | Extends the kill-test scripts in the project scratchpad. NOT built yet (see §7). |
| Phone capture tunnel (Vercel UI → Cloudflare Tunnel → Mac backend) | **Stretch / Cuttable** | Explicitly cuttable to folder-drop intake if not working by a hard internal cutoff tonight. Not a demo blocker. |
| Voice-logged mileage / invoice / "ask your books" | **Cut** | Not started. Out of scope for this build. |

Cut rule: if the phone tunnel is not working end-to-end by the internal cutoff tonight, we ship folder-drop intake and stop touching the tunnel. Core does not depend on it.

---

## 7. Architecture

Deliberately small stack — **two languages total** (Python + JS/TS) given the time remaining. No heavy frameworks, no build step on the frontend.

| Artifact | Language / tooling | Responsibility |
|---|---|---|
| Backend | Python / FastAPI | Intake queue, classification + extraction calls to the local model (via the §8 runtime adapter), binning logic, checklist state, correction persistence. **Also serves the frontend as static files** — the whole product runs from `localhost`, no hosting dependency. |
| Capture UI | Plain HTML/CSS/JS, static, no build step | Document submission (folder/file drop). Served locally by the backend. |
| Bin-review + checklist dashboard | Same plain-JS stack | Reviewer corrects extractions; per-client checklist view. Served locally by the backend. |
| Eval harness | Python | Extends the kill-test scripts (`eval/gen_w2.py`, `eval/run_test.py`). Scores doc-type and per-field accuracy over the labeled test set in `eval/testset/`. |

**Cut from the critical path (deliberate):** the Vercel-hosted UI + Cloudflare Tunnel existed only to serve the phone-capture stretch feature (an HTTPS page can't POST to a plain-HTTP local address). With phone capture cut to stretch (§6), the demo has **zero network dependency** — everything, UI included, serves from the Mac. This is also the demo's on-device proof: the product has zero network dependency, verified by grep (no non-localhost URL anywhere in frontend/backend/scripts). The staged Wi-Fi-off run was cut on demo day (see docs/TASKS.md T40). If phone capture is revived, the tunnel comes back with it.

INFERRED: the two-language constraint keeps context-switching and integration surface low, which is the right trade for a sub-24-hour build. The static no-build frontend also removes an entire class of deploy failures.

---

## 8. Model Runtime — Dual-runtime by design

The track disqualifies any project whose inference runs in the cloud, so the runtime choice is existential. Rather than betting on a single runtime, the backend reaches every model through **one adapter** (`backend/model_runtime.py`): a single function `extract(image_b64, prompt) -> str`, with the runtime selected by environment variable. Neither runtime can block the other's work.

**Runtime A — Ollama (VERIFIED, the default).**
`MODEL_RUNTIME=ollama`. `gemma4:e2b`, `gemma4:e4b`, and `gemma4:12b` are pulled and confirmed working on the demo machine via a real extraction test (§9), hitting `{OLLAMA_HOST}/api/generate`. This path works today. Development also happens from a secondary dev machine against this runtime (pointing `OLLAMA_HOST` at the model host over Tailscale).

**Runtime B — Courier OS (getcourier.ai) — BAKE-OFF RUN, FAILED. Not named anywhere public.**
`MODEL_RUNTIME=courier`. An MLX-native, Mac-only local model runtime with an OpenAI-compatible HTTP API. The adapter speaks its chat-completions shape, so trying it was a config flip. What verification found (two sessions):
- Fri overnight (VERIFIED): Personal edition runs fully self-hosted — no account, no login; the instance mints its own local API key. Its 8-bit `e4b` build (~14GB) hits a memory wall on this 24GB machine that Ollama's 4-bit (9.6GB) doesn't. The API *accepts* the OpenAI `image_url` payload shape — generation began; the stall was resources, not format.
- **Sat 9 AM bake-off (VERIFIED — the verdict):** `scripts/courier_bakeoff.sh` on an uncontended machine (Ollama models stopped, `ollama ps` empty, system reporting 70% memory free at request time). `/v1/models` answers and lists both `gemma4:e4b` and `gemma4:e2b` under matching ids. But the **e2b image sanity call — the smaller model — never completed: 3× 60s adapter timeouts, then a single direct call with a 240s timeout also timed out.** No response was ever received from image inference. The e4b kill test and 5-doc subset were therefore not reached (gated on sanity).

**Decision (final, per the pre-declared rule):** only a §9 kill-test pass permits naming Courier in the writeup or demo. It did not get past sanity. **Ollama is the sole named runtime everywhere public.** The dual-runtime adapter still ships — it is the honest architecture claim ("any OpenAI-compatible local server"), and the negative result is recorded here rather than erased.

**Sat ~12:50 PM — the pan-and-scan experiment (PARALLEL SESSION, in flight — not committed by the orchestrator, not shipped):** a second working session is actively optimizing the Courier path: `_panscan_views` in model_runtime.py (full page + overlapping crops per call, Courier-only, `COURIER_PANSCAN=0` to disable) with a results file showing `gemma4:e2b` via Courier at **20/20 fields, 0 silent wrongs, 5.35s median** on the 5-doc subset — including k1_clean_01 at 4/4 (which Ollama e4b fast scores 0/4) — and a companion file confirming the MLX E4B at 0% (vision-dead, matches the probe findings below). Orchestrator spot-check at ~12:50: e2b vision through Courier is REAL (read the actual employee name off w2_test, 27s); intermittent 500s during the check are attributed to CONTENTION with that session's own concurrent runs, not model failure. The parallel session owns model_runtime.py's courier path + eval/results_courier_* and commits its own work. The shipped demo path (MODEL_RUNTIME=ollama) is untouched either way — suite 88 green with the experiment in-tree. THE GATE STANDS: Courier gets named publicly only if e2b+panscan passes the §9 kill test + subset cleanly on an uncontended window before the writeup's final resubmit; Ollama remains the claim until then.

**Sat ~12:30 PM retest (VERIFIED — founder installed Courier's new `Gemma 4 E4B (mlx)` build; model list changed since the morning run):** the failure MODE changed, the verdict did not. The MLX build responds fast (14s first call, ~1-2s after) — but it never receives the image. Discriminating probes: with an image attached it replies "I need an image to process"; without one, same. Five payload shapes tried (OpenAI `image_url` object, `image_url` string, `input_image`, `type:image`, Ollama-style `images` array) — all dropped. Most damning: the very first probe didn't refuse — it FABRICATED a complete W-2 ("John Doe", "Acme Corp", `box2: 5000`) in flawless JSON, the exact silent-fabrication failure class this product exists to catch, caught by our kill-test in one call. Verdict unchanged: Courier cannot serve a vision product on this machine today; evals stay on Ollama.

- **Sat midday re-run (VERIFIED — overturns the morning's facts, not automatically its verdict):** the morning failure had three findable root causes, all fixed live: (1) the e2b image sanity hang was a worker crash — Courier's inference worker died on `'ascii' codec can't decode byte 0xe2` because the daemon had been relaunched from a shell with no `LANG`; restarting Courier with `LANG=en_US.UTF-8 PYTHONUTF8=1` fixed it (e2b sanity then passed in 14.3s with correct W-2 JSON through the adapter). (2) e4b's OOM gate was structural: Courier's 8-bit MLX e4b (11.5GB weights) demands 16.3GB free by its estimator, above what a 24GB Mac can offer — while the Ollama baseline runs Q4_K_M (4-bit), an unfair quant matchup in both directions. (3) Fix: pulled the quant-matched `FakeRockert543/gemma-4-e4b-it-MLX-4bit` (10.4GB) into Courier, workbench ctx 4096, and trimmed Courier's estimator cushions via its own env knobs (`UCE_MEMORY_SAFETY_PCT=0.02 UCE_WORKSPACE_BASE_MAX_GB=1.2 UCE_WORKSPACE_MULTIMODAL_GB=0.7` — actual load need ~12GB, guard passed at 13.4GB free with Chrome/Slack quit). Results, same 5-doc subset, same adapter: **kill test PASS (all six W-2 fields exact, 2.9s); doc-type 5/5 both runtimes; fields Courier 11/20 (55%) vs Ollama 13/20 (65%) on the same docs; median latency Courier 4.0s vs Ollama 18.4s — 4.6× faster.** Caveats that keep this from being an automatic PASS: the speed win comes with a 2-field accuracy drop on a 5-doc sample (noise-level n), the run required trimming Courier's safety margins and a quiet machine, and the 4-bit MLX build is a community quant, not the one Courier ships. **Naming verdict: Vin's call — the pre-declared kill-test gate is now met, but the accuracy delta is recorded.**

**Sat ~1:30 PM — FULL-RUN BAKE-OFF COMPLETE (VERIFIED, the definitive numbers):** full 32-doc eval, same harness both runtimes, pipeline default strategy (CASCADE/HAND_ENSEMBLE off — e4b only). **Courier e4b (`FakeRockert543/gemma-4-e4b-it-MLX-4bit`, pan-and-scan adapter): doc-type 32/32 (100%), fields 97/106 (91.5%), 7 silent wrongs, median 7.1s, handwritten gate 10/12 PASS. Ollama e4b (Q4_K_M, as-shipped): doc-type 29/29, fields 66/106 (62.3%), median 17.7s.** Courier wins fields by +29 points and latency by 2.5×. Same-model-family, same 4-bit class, same prompts, same scoring. Fairness caveat recorded: pan-and-scan (`_panscan_views` — full page + half/quadrant crops per call, each crop getting its own ~1500-token image budget) is applied in the Courier adapter path only; Ollama's API accepts multi-image too and an Ollama+panscan arm was NOT run — the +29 points is runtime-path vs runtime-path as they exist in the repo, not engine vs engine in isolation. Resolution of the 12:30 PM MLX fabrication finding: root cause was the `mlx-community_gemma-4-e4b-it-qat-OptiQ-4bit` repo shipping NO `processor_config.json` — Courier silently drops images it can't preprocess (hence fabrication); after patching the config in, the vision path runs but emits garbage (Courier can't dequantize OptiQ's vision layers) — that build is text-only in practice, `runs well` claims notwithstanding. Ops state for reproduction: Courier daemon = the GUI app's own (Courier OS.app), e4b workbench entry `Gemma 4 E4B 4bit` nickname `gemma4:e4b` ctx 8192 flex (8192 needed: K-1-class portrait docs use 5 views × 1500 tokens), e2b restored as before; adapter has 4× retry for Courier's transient eviction 500s. **All three pre-declared gates now pass (kill test, subset, full run). Naming verdict remains Vin's call; the panscan asymmetry is the one open fairness question.**

Note on the phone: a teammate's iPhone 14 (during Friday testing) loads `gemma4:e2b` in Google AI Edge Gallery but not `e4b`. That is why the phone never runs inference in this architecture — it is a capture peripheral only. All inference is on the Mac. (VERIFIED.)

---

## 9. Evidence & Evaluation

### The kill test (VERIFIED)

**What we ran.** A synthetic W-2 — typed and clean, deliberately easier than a real phone photo (stated limitation, see below) — sent to both `gemma4:e2b` and `gemma4:e4b` through Ollama's local API (`http://localhost:11434/api/generate`). Each model was asked to return strict JSON with six fields: `doc_type`, `employee_name`, `ssn`, `employer`, `box1_wages`, `box2_fed_withheld`. Scripts and the test image are in the project scratchpad (`gen_w2.py`, `run_test.py`, `w2_test.png`).

**Result (VERIFIED — reproduced identically across 3 independent runs, including one by a separate agent and one by the orchestrator directly):**

| Model | Fields correct | Failure | Latency (M4 Pro) |
|---|---|---|---|
| `gemma4:e2b` | 5 / 6 | Silently returned `70,110.00` for `box2_fed_withheld`; correct value is `9,183.44`. Appears to have grabbed a different box's value. | ~9.8-15s / doc |
| `gemma4:e4b` | 6 / 6 | None | ~18-21s / doc |

**Why this matters, stated honestly.** The `e2b` failure is the single most damaging failure mode for a tax-document product. It was not a refusal and not garbled output. It was a confident, clean, correctly-formatted, *wrong* number — indistinguishable from a right answer unless you already knew the truth. That is precisely the error a busy reviewer would miss. This finding directly justifies two product decisions:
1. **Use `e4b` over `e2b`** despite `e4b` being roughly 2x slower. Correctness on money fields outweighs latency.
2. **Build mandatory human review and correction into the product** rather than treating extraction as trustworthy on its own. Extraction is an assistant, not an authority.

### Stated limitation

The test document was cleaner than a real phone photo — no skew, shadow, glare, handwriting, or crumpling. The `e4b` 6/6 result is therefore a **best case**, not a representative one. Real messy-document accuracy is unknown.

### Planned eval harness (NOT built yet — top open item)

The real eval harness does **not exist as of this writing**. Planned design:
- 15-20 hand-labeled documents spanning W-2 / 1099 / K-1 / mortgage statements.
- A deliberate mix of clean scans, phone photos, and at least one handwritten example.
- Scores two things: doc-type classification accuracy, and per-field extraction accuracy.
- Built as an extension of the scratchpad kill-test scripts.

This is listed as the top open item, not as done. Until it runs, all accuracy claims rest on a single clean document.

### Design decision this evidence produced

The human-in-the-loop bin-review/correction step doubles as an ongoing evaluation signal. A **correction rate** per session (fields the reviewer had to fix / fields extracted) becomes a live accuracy metric — a continuous measurement, not just a one-time offline test. This turns the trust mechanism into a data source.

---

## 10. Design System

VERIFIED RENDER (updated Sat): the Claude Design mockup was retrieved and rendered locally — all four screens match this brief (paper/ink tokens, red-pen correction moment, yellow low-confidence flags, masked TINs, Caveat headlines). Mockup HTML + full-page render live in `docs/design/` (see `docs/design/DESIGN.md`).

Visual identity adapted from an existing Vin Jones project (ghostline.vinjones.me) — a "paper and ink" world:

| Token | Value | Use |
|---|---|---|
| Paper background | `#f7f5ee` | Warm base surface |
| Ink navy | `#1c2a3a` | Primary text |
| Ink blue | `#2f5fd0` | Primary accent; corrected values |
| Highlighter yellow | `#ffd24a` | Low-confidence / needs-review flags |
| Red pen | `#c0392b` | Human corrections only (struck-through wrong value) |

Type: **Caveat** (handwritten) for personality and headlines only; system sans with `tabular-nums` for all data and figures. Single theme by design — no dark mode. This is a committed visual world, not an omission.

**Three screens:**
1. **Capture / Submit** — kinetic, phone-first document submission.
2. **Bin Review & Correction** — the trust screen. It visibly shows the model's mistake being caught: the wrong value struck through in red pen, the corrected value in ink blue beside it. The correction is shown, not hidden.
3. **Checklist Dashboard** — the hero screen. A per-client missing-document checklist that visibly "inks itself in" as documents are confirmed. This is the one deliberate animation moment, chosen because it is the clearest visualization of the actual pain point (what is this client still missing), not the flashiest technical moment.
4. **Stats for Nerds** (STRETCH, added Sat morning) — rolling-24h live eval telemetry: correction rate ("the red-pen rate is the number to watch"), first-try classification accuracy, corrections by field category, docs/hour, median latency. This is PRD §9's "correction rate as live accuracy metric" promoted to a screen. Backend keeps a stringent append-only event log; the UI deliberately shows only the last 24h, and states that nothing leaves the Mac. Built only after the three core screens are green (docs/TASKS.md T14/T33).

**Copy note (2026-07-18, founder direction, T60):** two editorial lines were removed on purpose, not by drift. The privacy caption "Processed on this Mac. Nothing is uploaded." (Capture + Review) and the dashboard stats caption "measured on real documents" no longer earned their pixels. They are replaced by operational status computed from live data: a docs-awaiting-review count (Capture + Review), a friendly last-intake time (Capture + Dashboard stats line), and per-client progress fractions on the Dashboard cards — all derived from `/documents`, `/clients`, and `/stats`, in both live and mock mode. The Nerd Stats sentence "The dashboard shows the last 24 hours. Nothing leaves this Mac." is a distinct, retained statement and was intentionally left in place.

---

## 11. Rubric Self-Assessment

Rubric: Value 25 / Inputs & Data 15 / Enablement & Ease of Use 20 / Underlying Model 20 / Evidence & Evaluation 20.

| Category | Self-score | Justification |
|---|---|---|
| **Value (25)** | 21 | The compliance/liability framing is real and specific, and the users are nameable. Points held back until we can show it working on real messy intake, not just one clean doc. |
| **Inputs & Data (15)** | 11 | Handles the real input shape (photos/scans of tax forms) and real field extraction. Held back because the messy-document eval set is not built, so input robustness is unproven. |
| **Enablement & Ease of Use (20)** | 15 | Folder-drop is dead simple; the checklist directly answers the user's daily question. Depends on the phone capture path (cuttable) and on the UI actually rendering as briefed (unverified mockup). |
| **Underlying Model (20)** | 16 | Gemma 4 `e4b` verified 6/6 on a clean doc, fully on-device. Held back on the unresolved runtime decision (Ollama vs Courier OS) and unproven messy-doc performance. |
| **Evidence & Evaluation (20)** | 14 | The kill test is genuine, reproduced 3x, and drove a real design decision. Capped hard because the actual eval harness with a scored real/messy test set is not built yet. |

Honest note: the scores above depend on work **not yet done** — the eval harness, the phone capture path, and the final Courier OS decision. If those slip, Evidence, Inputs, and Enablement each drop.

---

## 12. Submission Checklist

Track: **On-Device AI with Gemma 4.** Cloud-based inference disqualifies the project — this is why §6 (model runtime) is existential.

| Item | Deadline | Status |
|---|---|---|
| Team registration | Sat 10:00 AM | Not done |
| Kaggle Writeup (product, model stack, GitHub link) | Sat 3:00 PM | Not done — **no writeup = ineligible regardless of demo quality** |
| New **public** GitHub repo, clean history, no prior/pre-existing code | Sat 3:00 PM | **Repo does not exist yet — must be created fresh after kickoff** |
| Live 3-minute demo | Sat 3:00-4:30 PM | Not done |

---

## 13. Open Risks

Pulled together and ranked, not buried.

1. **Courier OS image support still unverified — but no longer structural.** The dual-runtime adapter (§8) means trying Courier is an env flip on Vin's Mac, and the Ollama work on the secondary dev machine is never blocked by it. Remaining risk is only the claim: until Courier passes the §9 kill test, every public statement says Ollama.
2. **Real messy-document accuracy is unknown.** The clean-doc kill test is a best case. The eval harness (15-20 real/messy labeled docs) is not built. This is the top build item.
3. **Phone capture tunnel is unverified end-to-end** (Vercel UI → Cloudflare Tunnel → Mac backend). Explicitly cuttable to folder-drop by tonight's internal cutoff. Not a demo blocker, but currently unproven.
4. **Design mockup unverified against brief.** The Claude Design render could not be viewed (auth wall). §8 reflects the brief, not a confirmed render. Spot-check before final.
5. **New GitHub repo not yet created.** Must be a fresh public repo with clean history and no pre-existing code, created after kickoff, by Sat 3:00 PM.
