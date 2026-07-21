# KeepBook Roadmap — Phases

Phase labels adopted 2026-07-19. PHASE 1 = everything shipped for the hackathon demo (PRD.md is the historical record). PHASE 2 = the post-hackathon slate, chosen from the 2026-07-18 post-mortem (placed 4th; judge dock: "not enough interesting AI" — product read as a processing engine, not visible model autonomy) plus the honest backlog already recorded in PRD §5, IMPROVEMENTS.md, and TASKS.md.

Standing decisions (recorded 2026-07-18, post-mortem grill):
- Keep this codebase. No rebuild.
- Autonomous-action layer ships before new document types.
- Judge feedback is secondhand/vague — get a direct follow-up before sinking real time into anomaly detection specifically.

---

## PHASE 1 — Shipped (hackathon, 2026-07-17/18)

- Folder-drop intake; images only; big-seed demo states
- Classification: 6 core types (W-2, 1099-NEC/INT/MISC, K-1, 1098) with field extraction + 12 classify-only types; UNRECOGNIZED discipline (never force-fit); junk negatives held
- Per-client binning; client create/edit/guarded-delete; organizer-template chips
- Review & correction UI: red-pen strike + ink-blue correction with provenance, masked-TIN correction path, identity-confirmation gate, page numbers, zoom, un-confirm/re-open, delete/discard
- Per-client missing-document checklist (hero); strikethrough; count-aware matching; new-entry dot (T72)
- CSV export with correction provenance + CSV import
- Stats for Nerds: rolling-24h correction rate, per-field categories, latency, model-trace endpoint (`GET /documents/{id}/trace`)
- Eval harness: 29–32 labeled docs (clean / phone-photo / handwritten / junk buckets), silent-wrongs as first-class metric; conditional preprocessing pass; region-crop careful mode (`REGION_PASS=1`, 92.5%); HAND_ENSEMBLE flag; negative results recorded (re-ask, cascade, 12b)
- Dual-runtime adapter (Ollama default; OpenAI-compatible path + pan-and-scan; Courier bake-off record in PRD §8)
- Demo hardening: 60s timeouts, outage banners, honest queue, `/health` config+sha stamp, worker guards, snapshot-under-lock

---

## PHASE 2 — Next slate

Theme: **visible autonomy**. The model should be seen acting — flagging, noticing, drafting — not just extracting. Directly answers the "not enough interesting AI" dock while staying inside the human-gate philosophy (model proposes, human confirms).

### Tier A — Autonomous-action layer (build order as listed)

1. **Duplicate-submission detection.** Same doc submitted twice (emailed + photographed). Perceptual/near-dup match on intake, auto-flag with side-by-side compare, human resolves. Cheapest clearly-autonomous win; also closes IMPROVEMENTS #14 (zero-byte/dup uploads accepted silently).
2. **Cross-document anomaly flags.** Within a client's bin: SSN/TIN mismatch across docs, employer-name drift, tax-year mismatch, amount outliers vs prior docs. Flags surface in Review with a one-line model explanation ("why I flagged this"). Gate: confirm with a judge/user firsthand that this is the missing "interesting AI" before going deep.
3. **Watched intake folder (T70).** Point KeepBook at a folder; new files wake the pipeline unprompted. Ambient autonomy, visible in demo ("drop file in Finder, KeepBook notices").
4. **Auto-drafted "still waiting on" nudges.** Per-client draft generated from the checklist gaps; human sends. Ties autonomy to the hero feature.

### Tier B — Document-type expansion (after Tier A)

5. **Extended 1099/1098 family: classify-only → full extraction** (1099-DIV/-B/-R/-G, 1098-T/-E). Extends the existing eval harness; each type gets its own labeled bucket before the accuracy claim.
6. **Receipts + invoices** with line-item extraction (new field shapes: vendor, date, amount, category).
7. **Bank statements** (T66, promoted): transaction-level extraction — ACH/ATM/debit/deposit line items. Field signal (Rob's firm, 2026-07-18 email): clients no longer send check registers, they pay by ACH/ATM, so statements are the real substrate. Check registers demoted to classify-only, low priority. Unlocks reconciliation — where Tier A's anomaly flags grow into (matching statements against books, PRD §5 "data auditing").
8. **Sales reports** (new type, same email — "2025 Yosippity sales report.pdf"): business-client revenue summaries. Classify + summary-field extraction (period, gross sales, entity). Feeds the eventual P&L direction without committing to it.

### Tier C — Trust & ops hardening (parallel, small)

9. **PDF ingestion, incl. password-protected PDFs** (promoted — IMPROVEMENTS #6): render to images server-side; support encrypted PDFs with a locally-typed password (bank statements arrive as password-protected PDFs over email — Rob's firm, 2026-07-18). Password never persisted, decryption stays on-device.
10. **Real data retention** for `raws/` + `events.jsonl` (cleartext SSNs currently grow forever — IMPROVEMENTS #7, the known judge-question landmine).
11. **Remaining CRUD gaps** (CRUD-AUDIT.md): reassignment after confirm, expected_docs editing.
12. **Runtime default decision:** Courier/MLX path won the matched-technique bake-off (91.5% fields @ 7.1s vs Ollama 82.1% @ 22.7s). Decide default + naming (Vin's call, PRD §8), or fold pan-and-scan learnings back into the Ollama default.

### Real-world benchmark (standing, from Rob's firm)

Rob (azcpa.co) sent 3 real bank statements that Tanya is booking by hand — her books are ground truth. When bank-statement extraction (Tier B #7) lands, run KeepBook on the same statements and score against her result. Real client data: stays on the local machine only, never enters the repo or eval set (the repo's no-real-PII promise holds).

### Tier D — Beyond-Gemma model exploration (added 2026-07-19)

Hackathon track required Gemma; the product doesn't. Verified 2026-07-19:

13. **DeepSeek-OCR two-stage pipeline (the headline experiment).** DeepSeek-OCR = 3B VLM (MoE decoder, ~570M active params) built for token-efficient document OCR; already in the Ollama library (`deepseek-ocr:3b`, 6.7GB, 8K ctx, vision, needs Ollama ≥0.13). Proposed split: DeepSeek-OCR transcribes the page to text/markdown → Gemma e4b extracts fields from the *transcript, text-only* (no image tokens, no pan-and-scan multi-view). Expected wins: latency (text-only extraction is far cheaper than vision), plus the transcript becomes a human-readable intermediate for the trace view. Gate it like everything else: run the full 32-doc eval as a new arm vs the e4b-vision baseline; watch the handwritten bucket (third-party tests report DeepSeek-OCR variance rises on messy scans/handwriting) and prompt fragility (Ollama's own card: sensitive to input formatting). Alternates if it fails: PaddleOCR-VL (~0.9B, strong on tables/financial docs) and small Qwen3-VL variants.
14. **Multi-model scheduler in the runtime adapter.** Constraint: a model cannot be unloaded while tasks are in flight on it. Design: (a) per-model lease refcount in `model_runtime.py` — swap/unload requests queue until the outgoing model's in-flight count hits 0, and new tasks for it are held once a swap is pending (no starvation); (b) **stage-major batching** — run the whole batch through the OCR stage, barrier, swap, then the whole batch through extraction: one swap per batch instead of two per doc. (c) Check co-residency first: e4b Q4 (9.6GB) + deepseek-ocr (6.7GB) ≈ 16.3GB on the 24GB Mac — Ollama supports multiple loaded models (`OLLAMA_MAX_LOADED_MODELS`, unload via `keep_alive:0`); if both fit under real load, the swap problem mostly disappears and the scheduler is just the lease safety net.
15. **Re-run the runtime bake-off on Ollama ≥0.19.** Ollama's Apple Silicon backend moved to MLX (v0.19, Mar 2026) — the engine-level gap that Courier won on (MLX vs llama.cpp) may have closed. Item 12's default decision should use fresh numbers.

### Tier E — Split deployment: thin clients + one inference host (added 2026-07-19)

16. **In-firm split mode.** Front-desk machines run only a browser; one machine in the firm hosts KeepBook (backend + data + model). Already half-verified: the adapter reached a remote Ollama over Tailscale during the hackathon (`OLLAMA_HOST`, PRD §8), and the frontend is static files served by the backend. Build items: bind-address flag (localhost → LAN), shared-token auth (today there is none), multi-user review safety (two reviewers on the same doc — simple doc-level claim/lock), and access control around `raws/` (cleartext SSNs, now reachable by more people — pairs with Tier C #10 retention). Payoff: per-seat hardware cost collapses to "any machine with a browser" — including existing Windows front-desk PCs — and only the one inference host needs the HARDWARE.md tiers. Compliance story intact: the firm's own server is not a third-party processor (same logic as the WRITEUP durability answer).
17. **Externally-hosted variant — flag, don't fold in.** "Pay someone to host it" reintroduces exactly the third-party processor the product exists to eliminate — and entity type doesn't matter: an individual host is the same §7216 disclosure with worse practicals (no insurance, key-person risk). A zero-retention agreement is necessary but not sufficient — the disclosure happens at send-time regardless of retention promises, so firms likely need signed taxpayer consents (Treas. Reg. §301.7216-3), and the host gets swept into §7216's own criminal scope as an auxiliary-services provider. Legal shape needs a tax-practice attorney before any build. Three commercial shapes, ranked (2026-07-19 discussion):
    - **(a) Rented on-prem appliance — preferred (upgraded 2026-07-19).** Vin owns a fleet of Mac minis, rents one per firm priced by HARDWARE.md tier (16GB solo / 24GB standard / 32GB pipeline+fine-tune), sets it up on-prem. Inference stays in the firm; recurring revenue without a hosted disclosure. Design rules that make it clean: **one box per firm, never multi-tenant** (co-located firms would resurrect the processor problem — this also forces the fleet toward many small units, which is the better capital shape anyway); **Vin designed out of data access** — firm holds the FileVault key, management plane = signed update channel + health telemetry only (no document content), MDM scoped narrowly; **rented compute, firm-owned storage** — data folder on the firm's own external SSD, so returned/serviced boxes never carried client data. This shape plausibly fits the §301.7216-2(d) equipment/software-maintenance exception (written notice, no data path) — confirm with the same attorney pass as (b). Costs acknowledged: fleet capital, churn returns, fleet ops; setup visits double as white-glove onboarding (PRD §5).
    - **(b) Hosted inference with consent stack — separate track.** LLC + cyber insurance + own WISP + taxpayer-consent templates + technically enforced zero-retention. Never marketed under the local-only claim.
    - **(c) Fine-tune service via pattern-only pipeline.** NOT "send Vin your data" (bulk disclosure + PII can be memorized into weights). Instead: collect failure *patterns* from the correction log (form template, field, error class — no PII), reproduce them with the existing synthetic form generator, fine-tune on synthetic docs off-site, ship the LoRA back. Zero client data leaves the firm. Alternative: on-prem LoRA on their box (MLX vision-LoRA feasibility unverified — spike first).

### Deferred (not Phase 2)

- PWA / signed Mac app packaging (T68, PRD §5 distribution path) — matters at pilot stage, not before autonomy ships
- Relocatable data dir (T71), organizer-derived expected checklists (T67)
- Client-facing voice agent, P&L/balance-sheet generation, tax-software filing integration (PRD §5 — explicitly out, different accuracy bars/regulatory weight)
