# The Story Ledger — raw material for the demo, writeup, and judge Q&A

Everything we caught, prevented, or fixed tonight, recorded so no context window can eat it. Each entry: what happened → how it was caught → what it would have cost → the usable line.

## The narrative spine (use this framing)

**KeepBook exists because confident-looking wrong numbers slip past busy humans.** The e2b kill test produced one: a wrong federal-withholding value in clean JSON, indistinguishable from right. And then the entire build night turned out to be the same story at every layer — our test set lied confidently, our API lied confidently to our own dashboard, a research report lied confidently about a vendor. Every one was caught the same way the product catches them: **a verification loop that refuses to trust plausible output.** We didn't just build a product with that thesis; we built the product *using* its thesis. That symmetry is the pitch.

## Catch log (chronological)

1. **The kill test (Friday, pre-build).** Same synthetic W-2 to both on-device Gemma sizes. e2b: 5/6 fields, but federal withholding came back `70,110.00` instead of `9,183.44` — confident, well-formatted, wrong; reproduced 3×. e4b: 6/6. → Chose e4b despite 2× latency; made human review mandatory, not optional. *Line: "The dangerous failure isn't garbled output — it's a clean wrong number."*

2. **Repo was private.** Rules: public by 3 PM or ineligible regardless of demo quality. Caught during Friday-night repo wiring, flipped hours later. Would have cost: everything, silently.

3. **The `gemma4:cloud` trap.** Ollama's registry ships a cloud-inference tag one typo away from the local ones — instant track disqualification if used. Documented in the README as a do-not-pull warning before anyone could reach for it.

4. **The 2 AM eval bug (the centerpiece war story).** Provisional eval on the agent-generated 26-doc test set: 26/26 doc-type, **0/94 fields — for BOTH models.** The symmetry was the tell: models don't fail identically; harnesses do. Direct image inspection + a crop experiment found it: the generator rendered full 2200px IRS pages with the form in the top 40% — after the vision encoder downscales, field text is illegible (the giant "W-2" title survives, hence perfect classification). e4b was returning empty strings — *honestly reporting it couldn't read the values*. e2b was hallucinating the blank forms' printed box labels as answers — the confident-wrong failure again, this time exposing our test set. Plus a second bug: the W-2's SSN was overlaid outside its box. Codex wrote red tests pinning both; an agent fixed the generator, regenerated all 26 images, reran everything. *Line: "Our eval's first catch wasn't the model — it was our own test set lying to us. That's why you run evals before believing anything."*

5. **Bug → product feature.** The crop experiment that diagnosed the test set (cropping flipped 0 fields readable → most fields readable) revealed the model's sensitivity to input framing — which became the intake preprocessing module (crop/deskew/contrast). The test-set bug paid rent. *Line: "The best product feature we built tonight was discovered by debugging our own eval."*

6. **The multipart pin that caught a real bug.** Asked "are you confident frontend and backend will wire together?", we named the most likely killer precisely: the unpinned multipart field name on `/intake`. Read the built frontend (sends every file under the repeated key `file`), pinned it in the contract — and the backend build then hit exactly that landmine: its `form.values()` collapsed repeated keys, so N dropped files enqueued only 1. Fixed and verified with a 26-file folder drop before integration ever happened. *Line: "Name a risk precisely and it becomes checkable."*

7. **The port 8000 collision.** Courier OS's bundled database silently binds `localhost:8000` — the backend's contracted port. Found at 1 AM while setting Courier up; contract moved to 8100 the same hour. Would have cost: a mystery "backend won't start" during demo prep with both runtimes installed.

8. **The dashboard's confident lies.** First real-browser E2E of the integrated stack found the timeline endpoint returning sparse data the UI rendered as one giant bar and "undefined" labels — the backend was confidently emitting a shape the contract didn't say. Red tests → fix. *Line: "Even our own telemetry screen needed the review loop."*

9. **Courier reality vs. research.** Docs research said: account required, setup key required. Hands-on found: Personal edition runs fully self-hosted, no login, local API key minted by the instance itself. Also found its 8-bit E4B build (14GB) hits a memory wall on a 24GB machine that Ollama's 4-bit (9.6GB) doesn't — at 2 AM, not on stage. And its API *accepted* the OpenAI `image_url` shape (generation began; the stall was memory, not format). *Line: "Research reports what pages say; verification reports what machines do."*

10. **The scorer audit.** Before trusting the eval numbers, we audited the misses: exactly 1 of 53 was a normalization artifact ("L.L.P" vs "LLP"); the other 52 were real model errors. The numbers we'll show judges survived their own cross-examination.

11. **Teammate loss, absorbed.** Andrew left mid-hackathon and transferred the repo. Solo human + agent fleet absorbed the backend and eval lanes the same night, on a pinned contract that let frontend and backend be built by different agents and integrate on the first try. Ownership swept across every doc in one pass.

12. **The Wi-Fi-off demo proof.** Zero external requests by design — font vendored locally, no CDNs, Vercel and the tunnel amputated from the critical path. The on-device claim isn't a slide; it's zero external requests by construction. (The staged Wi-Fi-off beat was cut on demo day — T40; the no-external-URL grep is the standing evidence.)

13. **The silent-staleness trap that never fired.** Regenerating the test set would have silently kept the OLD broken photo variants — the augmenter skips files that already exist. Caught in the fix brief before it happened; stale variants deleted first. (Same species again: output that looks complete but isn't.)

14. **Process restart, nothing lost.** The orchestrating session died mid-night with three agents in flight — and every finished artifact survived because the workflow commits early and often. The task board's evidence rules meant the next session knew exactly what was true.

## The numbers (fill from final results before use)

- Kill test: e2b 5/6 with a silent wrong money value; e4b 6/6 (n=1, clean doc, 3× reproduced).
- 26-doc eval, e4b, post-fix baseline: 26/26 doc-type, 41/94 fields (clean 62%, synthetic-photo 26%), 23 silent wrongs, 17.2s median.
- e2b comparison: [pending — results_e2b file]
- Tool A/Bs (preprocessing; re-ask; cascade): [pending — keep only what beat its gate]
- Live product metric: correction rate surfaced in-app ("the red-pen rate is the number to watch").

**How to present the 43.6%:** it's the *baseline*, not the product — never apologize for it, and never leave it as the last word. Classification is perfect; extraction is fallible and fails *silently* on money fields — the entire argument for mandatory review. Then show the arc.

## The recursive improvement loop (ran until the 1 PM freeze)

Each round: run the eval → break misses down by failure class → aim one targeted, principled tool at the biggest class → re-measure → keep only what beats a pre-declared numeric gate. No prompt-hacking toward test answers; interventions must generalize (preprocessing, validation-triggered re-asks, model routing).

- Round 0: e4b 43.6% / 23 silent-wrongs; e2b 30.9% / 45 — the kill test at scale.
- Round 1: **conditional preprocessing SHIPPED** (photo 26%→64%, clean byte-identical by sha, silent-wrongs down; v1 failed the gate by degrading clean scans — the clean-detector iteration fixed it). **Re-ask REJECTED** (converted honest misses into well-formatted wrongs on free-text fields; zero correct values overwritten — the guard held, the concept failed). **Cascade REJECTED** (single-resident-model swap cost makes it structurally unwinnable on 24GB).
- Round 2: **region-crop pass: 92.5% fields, silent-wrongs 8 — failed the +6s latency gate at +10.5s, ships as "careful mode"** (`REGION_PASS=1`), an explicit measured tradeoff instead of a default.
- Wildcard: 12b's vision latency exceeds the 60s serving envelope — e4b beats both neighbors on the axes that matter.
- Handwriting ensemble (founder's 4 AM idea): dual-model read on handwritten docs; agreement-precision 75% vs 25% on disputes (n=12, preliminary); every handwritten field flagged regardless — the human gate is the feature.
- Final shipped numbers: fast 62.3% / 21 SW / 17.7s · careful 92.5% / 8 SW / 28.2s · classification 29/29 everywhere.

## 15. The handwritten SSN (the story to tell on stage)

At 3 AM the founder's wife hand-filled three forms with a stylus so the eval would include real handwriting. At import, the curator noted one risk in writing: her 3s have flat tops — easy to read as 5s. Hours later, e4b read her handwritten SSN as **457**-88-2210 instead of 437 — the exact predicted misread, in perfect JSON, on the highest-stakes field, with no error and no warning. The review screen caught it. A predicted failure, walked into on schedule, caught by the gate built for it: the entire product in one true anecdote. (Also delightful: the *small* model beat the big one on handwriting, 8/12 vs 7/12 — which is why the dual-model agreement idea has legs.)

**The flywheel (writeup future-section, real architecture):** every human correction in the review screen is a labeled example — the product generates its own eval data in normal use. Stats-for-Nerds is that flywheel's surface; nightly self-eval against accumulated corrections makes the recursion permanent after the hackathon.

## Q&A ammo (beyond docs/DEMO-SCRIPT.md crib)

- "What broke at 2 AM?" → Entry 4, verbatim. It's the best story we have.
- "How do you know your eval is right?" → Entries 4 + 10: the eval caught its own test set, then we audited its scorer.
- "Why should I trust the extraction?" → You shouldn't — that's the design. Review is mandatory, corrections are counted, and the correction rate is on a screen.
- "What did AI agents do vs. you?" → Agents built lanes against pinned contracts with binding definitions-of-done and evidence rules; humans set contracts, judged evidence, and caught what agents asserted. Same trust model as the product: generate fast, verify before believing.
