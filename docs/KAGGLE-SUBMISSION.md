# Kaggle Submission Kit — form fields + description

Everything below is copy-paste ready for the Kaggle form. Vin reads every word before pasting (his voice, his claims). Assets staged in `~/Desktop/keepbook-kaggle/`.

## Form fields

**Title:** KeepBook: on-device tax document intake for the firms that can't use cloud AI

**Subtitle:** Gemma 4 reads the documents, a human confirms every field, and no byte of a client's tax life ever leaves the laptop.

**Track:** Edge / On-Device Track

**Card / thumbnail image:**
- Wide card slot → `kaggle-1-dashboard.png` (the product in one glance: checklists, MISSING chips, correction count, "On-device only" badge)
- Square thumbnail slot → `icon-512.png` (logo mark on paper)

**Media gallery, in this order (captions ready):**
1. `kaggle-1-dashboard.png` — "The screen a bookkeeper lives in: per-client checklists that fill themselves in as documents are confirmed. What is every client still missing, at a glance."
2. `kaggle-3-redpen.png` — "The trust model: the model's wrong dollar value struck in red, the human correction in ink. Both preserved. Nothing enters a checklist unconfirmed."
3. `kaggle-2-review.png` — "Review & confirm: source document beside extracted fields, an explicit client-identity gate, SSN masked on screen with a deliberate reveal-to-edit control."
4. `kaggle-4-nerdstats.png` — "Stats for Nerds: live correction rate measured on the user's real documents, per-run model traces, all local. The red-pen rate is the number to watch."
5. `kaggle-5-capture.png` — "Intake: drop scans or photos, Gemma 4 classifies and extracts on-device, about 18 seconds a document."
6. `mockup-full.png` (optional) — "The design reference the product was built against."

**Links to attach:**
- Repo: https://github.com/mattej5/KeepBook
- Eval spec: https://github.com/mattej5/KeepBook/blob/main/docs/EVAL.md
- Full results: https://github.com/mattej5/KeepBook/blob/main/eval/results_final_e4b.json (plus `results_final_e2b.json`, `results_e4b_region.json` in the same folder)
- The runtime bake-off record, wins and failures both: https://github.com/mattej5/KeepBook/blob/main/PRD.md (§8)

## Project description (template)

### 💡 Inspiration

Every filing season, small CPA firms and solo bookkeepers drown in intake. Dozens of clients, each owing a shifting set of documents (W-2s, 1099s, K-1s, mortgage statements), arriving as email attachments, phone photos, and paper. The work is pure repetitive classification, and firms this size have had no safe way to automate it: a firm handling a stranger's SSN and wage history generally needs a signed data processing agreement with any processor touching that data, and no such agreement exists for "the chat tab someone had open at 9 PM." So the numbers get retyped by hand, or client identities quietly leak into unvetted cloud tools.

That's the problem we're solving today: automating the classification without routing a single client identity through a third-party processor. Local inference doesn't make a cloud tool safer. It removes the processor entirely. That's why this product can exist at all, and why the marginal document costs nothing during a bursty February week.

### 🛠️ How we built it

**Model:** Gemma 4 `e4b` (Q4_K_M, vision) running entirely on a MacBook M4 Pro through Ollama, with `e2b` as the comparison model. The backend reaches the model through a single adapter that also speaks the OpenAI-compatible local-server shape, so the runtime is swappable by env var without touching product code.

We picked the model with a kill test before building around it: the same synthetic W-2 to Gemma 4's two smallest on-device sizes, identical prompts, temperature 0. `e2b` read 5/6 fields but silently returned a wrong dollar amount for federal withholding in clean, confident JSON. `e4b` went 6/6. Reproduced three times. That one result drove both core design decisions: ship the larger model despite 2× latency, and make human review mandatory rather than optional.

The product around the model: a FastAPI backend (intake queue, classification, extraction, per-client binning, checklist state) serving a no-build HTML/CSS/JS frontend, localhost only, zero external requests. Every extraction passes a human who confirms or corrects it; wrong values are struck in red with the correction in ink, both preserved, so the correction rate becomes a live accuracy metric measured on the user's real documents. Anything unrecognizable lands in an explicit Unrecognized queue instead of being force-fit.

Then we made the eval drive the engineering. A labeled 29-document set (12 clean renders from official IRS form PDFs, 12 phone-photo degradations, 3 genuinely hand-filled forms, 2 junk documents): doc-type classification 29/29; field accuracy 62.3% in fast mode and 92.5% in careful mode; silent wrong values (a wrong value that looks like a right one, the failure class that actually hurts users) 21 fast / 8 careful, against 36 for `e2b`. An image-preprocessing pass shipped through its pre-declared gate. A region-crop pass beat both accuracy gates (+30 points, silent wrongs 21 to 8) but failed our interactive-latency gate at +10.5s a document, so it ships as an explicit careful mode for batch runs, not the default. Two other interventions (blind re-asking, a small-model cascade) were built, measured, and rejected by the same gates. The negative results are in the repo alongside the wins.

### 🚧 Challenges we ran into

The hardest hour was 2 AM, when our provisional eval came back 26/26 on classification and 0 of 94 on fields, for both models. The symmetry was the tell: models don't fail identically, harnesses do. Our own test-set generator had rendered full-page forms so small that, after the vision encoder downscales, the field text was illegible. The giant "W-2" title survived, hence perfect classification. And the two models failed differently in a way that proved the whole thesis: `e4b` returned empty strings, honestly saying "I can't read this," while `e2b` hallucinated the blank forms' printed box labels as answers. Confident wrong values again, this time exposing our own test set. We wrote red tests pinning the bug, fixed the generator, regenerated all 26 images, and reran everything before believing a single number.

The rest of the hard parts were discipline under a one-day clock: holding a pre-declared rule that no runtime gets named publicly until it passes our kill test (the full bake-off record, including what failed, lives in PRD §8 of the repo); rejecting our own accuracy interventions when they failed latency or silent-wrong gates; and freezing features just before 1 PM so the last two hours belonged to evidence, rehearsal, and honest documentation instead of code.

---

## OPEN ITEM before submit (Vin's call)

The writeup's runtime line currently says the second runtime "did not pass our image-inference verification... so per our pre-declared rule it is not named." As of ~1:30 PM the parallel session's full 32-doc bake-off PASSED all three pre-declared gates (kill test, 5-doc subset, full run): 32/32 doc-type, 97/106 fields (91.5%), 7 silent wrongs, 7.0s median via a quant-matched MLX 4-bit build + pan-and-scan adapter, vs our shipping 62.3% / 17.7s. Recorded caveat: pan-and-scan ran only on the Courier path (no Ollama+panscan arm), community quant, trimmed memory margins. The stale sentence must change either way. Two drafts:

**Option A (name it):** "…The adapter also implements the OpenAI-compatible local-server shape, which let us bake off a second Mac-native runtime, Courier OS, against Ollama through the same adapter on demo day. Its as-shipped build failed image inference on 24GB hardware in the morning; by early afternoon a quant-matched 4-bit MLX build plus a pan-and-scan adapter passed all three of our pre-declared gates: 91.5% fields at 7.0s median on the full eval. The full record, including the fairness caveat that pan-and-scan ran only on that path, is in PRD §8. The demo ships Ollama."

**Option B (describe, don't name):** same text with "a second Mac-native MLX runtime" in place of the name.
