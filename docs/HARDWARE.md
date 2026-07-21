# KeepBook Hardware Requirements

Tracks what hardware the current version actually needs. Claims labeled VERIFIED (measured on the reference machine) / INFERRED (derived from model sizes or specs, not yet run on that hardware). Update this doc whenever the model stack, runtime, or Phase 2 pipeline changes RAM math.

**Reference machine (all published numbers measured here):** MacBook M4 Pro, 24GB unified memory, macOS 26. Every latency/accuracy figure in PRD §8–9 and WRITEUP.md comes from this machine and no other. (VERIFIED)

---

## Why RAM is the binding constraint

Local vision inference holds model weights + KV cache/context in unified memory alongside macOS and the app:

| Component | Size | Status |
|---|---|---|
| Gemma 4 `e4b` Q4_K_M (Ollama, shipped default) | 9.6GB weights | VERIFIED |
| Context at 8192 (pan-and-scan: K-1-class portrait docs use 5 views × ~1500 image tokens) | ~1–2GB | VERIFIED (ctx requirement), INFERRED (exact GB) |
| Courier MLX 4-bit `e4b` alternative | 10.4GB weights, ~12GB load; passed its memory guard at 13.4GB free | VERIFIED |
| Gemma 4 `e2b` (comparison/ensemble only) | ~5GB runtime | VERIFIED |
| macOS + browser UI headroom | ~4–6GB realistic | INFERRED |

Known hard ceilings already hit on 24GB (VERIFIED): Courier's 8-bit e4b (11.5GB weights, 16.3–19.9GB by its estimator) does not fit — the 4-bit quant was required; `gemma4:12b` fits but blows the 60s latency envelope, so more RAM alone doesn't unlock it.

---

## Tiers

### Floor — 16GB Apple Silicon (INFERRED, NOT YET TESTED)
- Runs: `e4b` fast mode, single resident model, nothing else heavy open.
- Math: 9.6GB weights + ctx + OS ≈ 15–16GB — workable but tight; careful mode (REGION_PASS) and big-context panscan may be marginal.
- Rules out: model co-residency (Phase 2 Tier D), HAND_ENSEMBLE dual-model mode, Courier 8-bit builds.
- **Open validation task: no 16GB run has ever happened. Borrow/test one before publishing "16GB minimum" anywhere.**
- PRD §5's first-run wizard already specifies the enforcement: hardware check (Apple Silicon, ≥16GB) + self-run kill test at install.

### Recommended — 24GB Apple Silicon (VERIFIED — the reference config)
- Everything shipped works as measured: fast mode 17.7s median, careful mode 92.5% @ 28.2s, Courier path 91.5% @ 7.1s.
- Fits Phase 2 two-stage co-residency on paper: e4b Q4 (9.6GB) + `deepseek-ocr:3b` (6.7GB) ≈ 16.3GB + ctx + OS — plausible but tight, must be load-tested (INFERRED).
- This is the honest "buy this" tier for a firm.

### Comfortable — 32GB+ Apple Silicon (INFERRED)
- Headroom for: co-resident OCR+extraction pipeline without swap scheduling, dual-model ensemble as default, larger context, firm-sized batch runs with the machine still usable.

### Performance note within a tier (INFERRED)
Inference latency is roughly memory-bandwidth-bound. Reference numbers are M4 **Pro** (273GB/s); a base M4 (120GB/s) at the same RAM should hit the same accuracy at meaningfully higher latency — untested, worth measuring once a base-M4 machine is available.

---

## Disk

- Models: e4b (9.6GB) + e2b (~8GB) + optional deepseek-ocr (6.7GB) → budget **~30GB free** for the model set. (VERIFIED sizes, INFERRED total with update slack)
- Product data: one folder, small (images + JSONL events + raws). Time Machine covers it with zero config. (VERIFIED)

---

## Explicitly NOT supported for inference

- **Phones.** iPhone 14 loads `e2b` in Google AI Edge Gallery but not `e4b` (VERIFIED, Friday hackathon test) — and e2b failed the kill test (silent wrong money value). Phones are capture peripherals only, by architecture.
- **Intel Macs.** No MLX; llama.cpp CPU-only vision inference is far outside the latency envelope (INFERRED — never attempted; Andrew's i5 laptop was dev-only against a remote Ollama host).
- **Windows.** Door held open by the runtime adapter (Ollama is cross-platform) and commercially it matters — bookkeeping is QuickBooks/Windows country (PRD §5) — but zero verification. Needs its own bake-off on real Windows hardware (RAM math differs: discrete-GPU VRAM vs unified memory) before any claim.

---

## What to tell a business owner (buyer guidance)

Most firms' existing machines won't have the RAM. The good news: the required hardware is one commodity desktop, not a server.

- **Entry:** Mac mini M4, 16GB/512GB — $799 as of May 2026 (Apple raised the base config; street prices dip lower). Only after the 16GB floor validation passes.
- **Recommended:** Mac mini M4, 24GB — ~$999 list, matches the verified reference tier. One machine serves the whole firm: KeepBook is a local web app, so other computers in the office can use it over the LAN while data stays on that one box (LAN serving itself: INFERRED, currently bound to localhost — needs a config flag + the compliance story stays intact since the firm's own machine isn't a third-party processor).
- Framing for the pitch: ~$1k one-time vs a per-token cloud bill that is wrong-shaped for seasonal bursty intake (WRITEUP.md economics argument) — the machine pays for itself in the first filing season and doubles as a normal office computer.
- **Rental alternative (ROADMAP #17a):** firms can rent the box from us instead of buying, priced by tier, set up on-prem. One box per firm (never multi-tenant), firm holds the FileVault key, data folder on firm-owned external SSD — returned hardware never carried client data.

Sources for pricing: [MacRumors — Mac mini starts at $799](https://www.macrumors.com/2026/05/01/mac-mini-now-starts-at-799/), [Apple — Mac mini](https://www.apple.com/shop/buy-mac/mac-mini).

---

## Split deployment: thin clients + one inference host (ROADMAP Tier E, not yet built)

The single-machine tiers above assume one computer does everything. Split mode changes the math: **only the inference host needs the tiers above; every other seat is a browser.**

| Role | Requirement | Status |
|---|---|---|
| **Client seats** (front desk, reviewers) | Any machine with a modern browser — 8GB RAM fine, OS irrelevant (existing Windows PCs and Chromebooks qualify). Zero model downloads, zero install beyond a URL. | INFERRED (frontend is static files already served by the backend — VERIFIED single-user; multi-user untested) |
| **Inference + app host** (one per firm) | The 24GB Recommended tier above. Holds backend, data folder, and model. All client data lives only here. | VERIFIED as single-machine config; remote-client serving untested |
| **Network** | LAN. Document images are a few MB per upload; per-call model traffic stays on the host. Remote adapter path itself is proven — hackathon dev ran the backend against Ollama on another machine over Tailscale (`OLLAMA_HOST`). | VERIFIED (adapter over network), INFERRED (full app over LAN) |

Gaps before this is real (ROADMAP #16): LAN bind flag (today localhost-only), authentication (today none), doc-level locking for concurrent reviewers, access control + retention for `raws/`. Concurrency note: the worker queue is sequential and the host runs one resident model — multiple users share one inference lane, so batch latency adds up at busy hours (acceptable for intake; measure before promising).

**Compliance line, drawn explicitly:**
- **In-firm host: story intact.** The firm's own server is not a third-party processor. Same argument as pointing the data folder at the firm's file server.
- **Externally hosted ("pay someone to host it"): different product.** A third-party processor exists again — the exact thing the local-only pitch eliminates. Viable only as a separate managed offering with a signed DPA, dedicated tenancy, and transit encryption, and never sold under the "no byte leaves the building" claim (ROADMAP #17).

Buyer guidance update this enables: a 5-person firm needs ONE ~$999 Mac mini (24GB), not five — existing front-desk machines become the seats.

---

## Standing validation list

1. 16GB floor run (full 32-doc eval + one live session) — blocks any public "minimum 16GB" claim.
2. 24GB co-residency load test (e4b + deepseek-ocr resident together under real intake) — decides whether the Tier D swap scheduler is a safety net or a requirement.
3. Base-M4 (non-Pro) latency measurement.
4. Windows/discrete-GPU feasibility spike — only when Windows port becomes real.
5. Split-mode LAN test: 2–3 browsers on other machines against one host under simultaneous intake (blocks the one-Mac-mini-per-firm pitch).
