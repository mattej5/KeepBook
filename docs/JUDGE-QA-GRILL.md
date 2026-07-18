# Judge Q&A — the hard eight (grill-agent output, evidence-grounded)

Prep notes for Vin. Every answer traces to repo evidence; cautions marked. Read alongside the DEMO-SCRIPT crib.

**Q1. "62.3% field accuracy. Why would anyone use this?"**
62.3% is the model's unaided read on deliberately degraded synthetic photos — it is not the product's accuracy. Nothing enters a checklist unconfirmed, so product-level accuracy is the human gate's, and the product measures its own error rate live: every red-pen correction is counted and on a screen. Extraction is an assistant, not an authority.
⚠️ CAUTION: the "~5× faster than typing" figure (writeup + crib) is unsourced in the repo. Say "verifying is much faster than retyping" unless you want to own it as a personal estimate out loud.

**Q2. "You have 92.5% in the repo. Why isn't careful mode the default?"**
Pre-declared gates, applied even when the result was impressive. Region-crop beat both accuracy gates (+30.2 points, silent wrongs 21→8) but failed the latency gate: +10.5s/doc, 28.2s median, against a declared +≤6s ceiling — structurally unreachable here because name fields always get a crop re-read. So it ships as `REGION_PASS=1` for batch use. Holding the gate against our own best result is the eval discipline working.

**Q3. "PRD §8 shows a runtime doing 91.5% at 7s, and even e2b hitting 20/20 with pan-and-scan. Why ship 62.3% at 18s — and isn't your comparison unfair?"**
Three honest parts. (1) The Courier pass landed ~1:30 PM, after freeze, and needed a community 4-bit quant, trimmed memory-safety margins, and a quiet machine — the shipped path is the one verified stable end-to-end. (2) The fairness asymmetry is recorded in our own words in PRD §8: pan-and-scan ran only on the Courier path; no Ollama+panscan arm; so +29 points is path-vs-path, not engine-vs-engine. (3) The e2b 20/20 is n=5 docs on the two easiest buckets, never the full 32. What panscan really shows: image resolution is a huge lever for both models — which is why preprocessing and region crops are the two interventions that shipped.

**Q4. "Doesn't e2b-with-panscan mean your kill test measured resolution, not model quality?"**
No — the kill test fed both models the identical, legible image (e4b read all six fields). e2b still substituted a different box's value for federal withholding, three reproductions. The full eval repeats the identical-input comparison at n=106 fields: 66 vs 40 correct (roughly z=3.6, p<0.001). Panscan helping e2b doesn't make its same-input silent-wrong behavior acceptable for a tax product.

**Q5. "What's your handwriting story?"**
Three genuinely hand-filled forms, values pre-committed on a fill sheet before pen touched screen. e4b fast reads 7/12 (58%), careful mode 9/12, and the misses are human-plausible — a flat-topped handwritten 3 read as a 5 in an SSN, the exact confusion predicted at import. Policy: handwritten docs are ALWAYS flagged for full review. The dual-model cross-check (75% agreed vs 25% disputed, n=12, explicitly preliminary) makes flags smarter, never replaces the gate.
⚠️ If pressed: e2b happened to score 8/12 on that bucket — at n=12 that's noise (p≈0.67), and the policy doesn't depend on which model wins the bucket.

**Q6. "Why not cloud with a signed DPA?"**
Concede, then differentiate. Enterprise intake vendors with DPAs exist (SurePrep, Gruntworx) — built and priced for firms with procurement teams. For a solo bookkeeper the realistic cloud option is a consumer chat tab, which is exactly the uncontrolled-liability path. Even a DPA'd vendor is still a third-party processor the firm must vet, monitor, and disclose; on-device removes the processor question rather than managing it. And intake is bursty (a February week), so per-token billing is wrong-shaped; the marginal on-device document is free.

**Q7. "Your eval set is self-generated and mostly synthetic. Why trust it?"**
Openly a limitation, mitigated three ways: labels emit in the same code path as the images (ground truth can't drift); the harness's first catch was our own generator lying to us (0/94, caught by symmetry, pinned with red tests, all images regenerated); and the three handwritten docs are real pen strokes. Honestly missing: the real-phone-photo bucket (T23, listed undone on the task board, not hidden). The production answer to eval bias is the product: mandatory review turns every real document into a labeled example, and the correction rate is the live metric on the user's actual data.

**Q8. "What breaks at 500 clients?"**
Not inference first. 500 clients × ~8 docs ≈ 4,000 documents; at 17.7s that's ~20 GPU-hours across a season — overnight batches on one Mac (careful mode ~31h). What breaks first is persistence and workflow: state.json is one atomically-rewritten JSON document, the intake queue is sequential, one reviewer, no auth or multi-user, and uploads/ plus the event log grow unbounded. Honest position: single-preparer v0; recorded roadmap is a relocatable data dir pointed at the firm's own file server (T71 — preserves the no-third-party thesis) and SQLite behind the same API.
