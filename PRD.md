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

**Cut from the critical path (deliberate):** the Vercel-hosted UI + Cloudflare Tunnel existed only to serve the phone-capture stretch feature (an HTTPS page can't POST to a plain-HTTP local address). With phone capture cut to stretch (§6), the demo has **zero network dependency** — everything, UI included, serves from the Mac. This is also the demo's on-device proof: the full flow runs with Wi-Fi off (see docs/TASKS.md T40). If phone capture is revived, the tunnel comes back with it.

INFERRED: the two-language constraint keeps context-switching and integration surface low, which is the right trade for a sub-24-hour build. The static no-build frontend also removes an entire class of deploy failures.

---

## 8. Model Runtime — Dual-runtime by design

The track disqualifies any project whose inference runs in the cloud, so the runtime choice is existential. Rather than betting on a single runtime, the backend reaches every model through **one adapter** (`backend/model_runtime.py`): a single function `extract(image_b64, prompt) -> str`, with the runtime selected by environment variable. Neither runtime can block the other's work.

**Runtime A — Ollama (VERIFIED, the default).**
`MODEL_RUNTIME=ollama`. `gemma4:e2b`, `gemma4:e4b`, and `gemma4:12b` are pulled and confirmed working on the demo machine via a real extraction test (§9), hitting `{OLLAMA_HOST}/api/generate`. This path works today. Development also happens from a secondary dev machine against this runtime (pointing `OLLAMA_HOST` at the model host over Tailscale).

**Runtime B — Courier OS (getcourier.ai) (supported in code, UNVERIFIED in practice).**
`MODEL_RUNTIME=courier`. An MLX-native, Mac-only local model runtime with an OpenAI-compatible HTTP API — likely built by one of the judges, a real affinity signal. Because the adapter already speaks the OpenAI chat-completions shape, trying Courier is a config change on Vin's Mac, not a code change. Still not verified:
- Whether it serves the exact `gemma4:e4b` variant. (ASSUMED unknown.)
- Whether its API accepts image input at all — the load-bearing unknown, since the whole product is image extraction. (ASSUMED unknown.)
- Account install/auth has not happened yet.

**Decision rule (explicit):** the adapter ships both runtimes; the honesty gate is unchanged. The demo and Writeup claim whichever runtime **passed the §9 kill test on the demo Mac**. Courier OS must pass that test with equal or better results before it is named anywhere. If both pass, demo on the better score/latency; Ollama remains the guaranteed fallback. As of this writing the verified runtime is **Ollama**.

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
