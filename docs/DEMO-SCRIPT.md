# Demo Script — 3:00 flat

Hybrid structure per docs/USER-JOURNEY.md: 30s story cold-open → feature tour → evidence → close. Verbatim lines in quotes; stage directions in brackets. Rehearse with a stopwatch (TASKS T50); cut lines from the middle, never the close.

## Pre-demo checklist (T53)

- [ ] Backend + frontend running; one warm inference completed < 5 min before slot
- [x] Demo folder staged: `~/Desktop/keepbook-demo/` — 6 docs picked from eval per-doc results (results_final_e4b.json):
  - `w2_clean_02.png` — 5/5 fields correct → the "fifteen seconds" clean-confirm beat (pre-verified per IMPROVEMENTS #8)
  - `1099int_clean_01.png` — 3/3 correct
  - `w2_photo_01.png` — **money correction plant**: reads box2 fed withholding `6,410.79` for true `10410.79` — the script's "read the wrong box" line, verbatim
  - `1098_clean_01.png` — **name correction plant + careful-mode flip beat**: lender reads `Coppell Bank` for `Copperline Bank` (consistent across 3 variants); this is the exact doc REGION_PASS fixes if you do the live flip
  - `1099int_photo_01.png` — 2/3 + 1 honestly-flagged miss (shows the low-confidence flag)
  - `receipt_01.png` — the UNRECOGNIZED plant
- [ ] Seed state loaded via `./scripts/demo_state.sh big` — the 8-client seed (reads as real software); it preserves the canonical trio and their exact script states: Ruth Okafor (one confirm from complete), Marcus Whitfield (missing 1099-INT), Chen partnership (missing K-1 + 1098). Fallback restore (`fallback`) unchanged.
- [ ] Fallback restore command sitting in a ready terminal (T43)
## Script

**0:00 — Cold open** [checklist dashboard on screen, but don't explain it yet]

> "It's February 12th. Maria does the books for sixty clients, and nine tax documents landed in her inbox overnight. Tonight she has two options: retype every number by hand, or paste a client's Social Security number into some chat tab — a cloud AI she has no data processing agreement with. Most bookkeepers quietly pick option two. That's the problem."

**0:20** [drag the demo folder into the drop zone — processing starts NOW and runs behind the rest of the demo]

> "So we built KeepBook. I just dropped her morning inbox in. Everything you're about to see — the model included — is running on this Mac."

**0:35 — Capture screen** [point at the on-device banner, then the queue filling]

> "Gemma 4 — the e4b model, running locally through Ollama — is classifying each document, extracting the fields, and sorting them into per-client bins. About twenty seconds a document, so they'll keep landing while we talk."

**1:00 — Bin Review screen** [open a processed doc: image left, fields right]

> "Nothing gets trusted without a human. Here's a W-2 — the model read it, Maria confirms it. Fifteen seconds." [confirm the clean one]
> "And here's why the review step is mandatory —" [open the correction plant] "— the model read the wrong box for federal withholding. Watch: strike the wrong value, type the right one." [red strike, blue ink] "That mistake just died here, at 8 AM, instead of inside a filed return in April."

[flash the receipt in Unrecognized]
> "And when someone emails a receipt by mistake? It says 'I don't know what this is' instead of confidently guessing. In this product, honest confusion beats confident nonsense."

**1:45 — Checklist Dashboard** [confirm the last Okafor doc → row inks in]

> "This is the screen Maria actually lives in. Ruth Okafor just went complete — watch it ink itself in. The Chen partnership is still missing a K-1 and a mortgage statement — so Maria sends that chase email in February, not in a panic on April 10th. This list is the product: 'what is every client still missing,' answered at a glance."

[point at stats line]
> "And every correction she makes is counted. That correction rate is a live accuracy meter, measured on her real documents — not our benchmark."

**2:20 — Evidence**

> "We didn't guess the trust model — we measured it. Same synthetic W-2 to Gemma's two smallest on-device sizes: the 2-billion model returned a wrong dollar amount for tax withheld — clean JSON, confident, indistinguishable from right. The 4-billion model got six out of six. So we built a 29-document eval — clean scans, phone photos, junk documents, and real handwriting: classification twenty-nine for twenty-nine, fields 62 percent in fast mode — 92 in careful mode — and the silent-wrong-value count is the number we watch: twenty-one fast, eight careful, thirty-six for the small model."

**The SSN story** [tell this one with relish — it's true and it's the whole product in 20 seconds]:

> "My favorite result from last night: at 3 AM, my wife hand-filled tax forms with a stylus so we could test real handwriting. When we imported them, we noted one risk: she writes her 3s with a flat top — easy to mistake for a 5. Hours later the model read her handwritten SSN... and made *exactly* that mistake. 457 instead of 437. Perfect JSON. Wrong identity. No error, no warning — unless you have a review screen. We predicted the failure, the model walked into it, and the red pen caught it. That's why the human gate isn't a fallback in this product. It's the product."

**2:45 — Close**

> "Small firms handling strangers' SSNs were never going to get a data processing agreement from a chat tab. KeepBook doesn't make cloud AI safer — it removes the cloud entirely. Every byte of every client's tax life stayed on this MacBook. That's KeepBook. Thanks."

**Optional careful-mode beat** (rehearse before deciding — it costs ~40s): the flag is read from the process env, so the flip is a server restart (state persists, model stays warm in Ollama, ~2s):
```
pkill -f "uvicorn main:app"; cd backend && REGION_PASS=1 nohup .venv/bin/python -m uvicorn main:app --port 8100 > uvicorn.log 2>&1 & cd ..
```
Then re-drop `1098_clean_01.png` → the lender that read "Coppell Bank" in fast mode comes back "Copperline Bank" (~28s careful). Line: "Same document, careful mode — watch the wrong bank name fix itself. Ten more seconds a document, eight silent wrongs instead of twenty-one. We made that a mode, not a default, and we can tell you exactly why."

**If live processing stalls:** run the fallback restore [T43], say "let me jump to a session from this morning," continue at 1:00. Do not debug on stage.

---

# Judge Q&A crib

**"Why not just OCR — Tesseract, or AWS Textract?"**
Textract and Document AI are cloud — the exact DPA problem we exist to remove. Local OCR gives you characters, not answers: it won't classify a form, map "Box 2" to federal withholding, or survive a skewed phone photo with a shadow. Gemma reads the document the way a person does — layout, labels, context — and returns structured fields.

**"How do you know it's accurate? What about hallucination?"**
We assume it isn't, and built the product around that. Our own kill test caught the small model hallucinating a dollar figure in perfect JSON. Three answers stacked: (1) the eval harness — 29 labeled docs, scored per-field, silent-wrong values tracked as their own class; (2) mandatory human review — nothing enters the checklist unconfirmed; (3) the correction rate — a live accuracy metric on the user's real documents, forever.

**"Why e4b? Why not the smallest model, or the biggest?"**
We ship the smallest model that passed our kill test. e2b failed it — silently wrong money value. e4b passed 6/6 at ~20s/doc on this hardware. We didn't ship 12b because we have no evidence it buys accuracy we need, and it costs latency we'd feel.

**"What about handwriting / really messy documents?"**
Tested honestly with real pen-filled forms: the model reads handwriting at 58% of fields — and its misses look like a careless human's ("Celesle" for "Celeste", a flat-topped 3 read as 5). Our policy: handwritten documents are *always* flagged for full review, no exceptions — but even so, confirming a pre-filled field takes a couple of seconds while typing it takes fifteen. The model is the typist; the reviewer stays the accountant. [If the ensemble experiment landed: we also cross-check handwriting with two model sizes — agreement raises confidence, disagreement raises the flag — but the human gate stays regardless.]

**"Isn't 60-something percent extraction bad?"**
It would be, if we hid it. Instead it's the design input: extraction is fallible, so review is mandatory, corrections are one click, and the correction rate is on a screen. The economics still work — verifying a filled field is much faster than retyping it, so even correcting a third of fields, the bookkeeper finishes far ahead of manual entry. And the alternative wasn't better AI, it was no AI at all.

**"Who pays for this?"**
The firm — per-seat, seasonal peak. The buyer is the person liable for the data: solo bookkeepers, small CPA firms. For a firm this size the realistic cloud option is a consumer chat tab they can't take liability for — enterprise intake vendors with DPAs exist, but they're built and priced for firms with procurement teams. So the alternative isn't a cheaper competitor — it's retyping.

**"Isn't this just a wrapper around Gemma?"**
Gemma is the reader. The product is what surrounds it: binning, the per-client checklist, mandatory review with correction provenance (struck value preserved), the Unrecognized path, and an eval harness that measures the whole pipeline. Swap the model next year; the product remains.

**"Can the reviewer fix a misread SSN? It's masked on screen."**
Yes — that gap got closed the morning of the demo. Masked SSN/TIN fields carry a quiet pen "edit" control: activate it and you get the cleartext value to correct (you're looking at the source image anyway), and once confirmed, the provenance renders with BOTH the struck original and the correction masked — the audit trail shows a digit was fixed without ever printing two SSNs on screen. Server-side the real values persist cleartext (the CSV export needs them); masking is deliberately a display-layer choice.

**"It's all local — what happens when the laptop dies? Where's the backup?"**
The data footprint is deliberately one folder with atomic crash-safe writes, so today it's covered by Time Machine with zero configuration — most firms' Macs already have that. The roadmap answer is a one-line setting pointing that folder at the firm's own file server or NAS. Note what that preserves: a firm's own server is not a third-party processor, so backup never reintroduces the data-processing-agreement problem the product exists to remove. Durability without a cloud dependency.

**"Why no QuickBooks integration?"** [researched morning-of; every claim verified against current vendor docs]
QuickBooks is bookkeeping software, not tax-document software — its API has no object for a W-2 or a K-1, so the only thing we could push is a PDF into its generic attachments bucket, which doesn't capture the extracted field values that are the whole point. The tools where our structured data is actually worth money are the tax-prep engines — UltraTax, Lacerte, Drake — and those are closed: no public API, only proprietary partner integrations. So we ship CSV today, which everything ingests, and the API roadmap names the practice-management systems that actually have open APIs: Karbon and Canopy. [Do NOT name TaxDome as an API target — it has no public API, Zapier-only, and Zapier can't even receive a document.]

**"Does it file taxes?"**
No, and deliberately never claims to. It organizes and verifies intake for a human preparer. Filing is licensed territory — we stay on the safe side of that line (PRD §5).

**"Why Gemma 4 specifically?"**
Vision-capable at sizes that genuinely fit on-device — e4b is 8B params quantized, ~9.6GB, comfortable in 24GB unified memory with room for the OS. Native vision means no OCR pipeline. And the on-device requirement isn't a track constraint we tolerated — it's the product's entire reason to exist.

**"What broke at 2 AM?"** [the best story we have — tell it straight]
At 2 AM our provisional eval came back 26/26 on classification and **0 of 94 fields — for both models**. The symmetry was the tell: models don't fail identically, harnesses do. We inspected the images and found our own test-set generator had rendered full-page forms so small that, after the vision encoder downscales, the field text was illegible — the giant "W-2" title survived, hence perfect classification. And the two models failed differently in a way that proved the whole thesis: e4b returned empty strings — honestly saying "I can't read this" — while e2b hallucinated the blank forms' printed box labels as answers. Confident wrong values again, this time exposing our own test set. We wrote red tests pinning the bug, fixed the generator, regenerated all 26 images, and reran everything. Our eval's first catch wasn't the model — it was our own test set lying to us. That's why you run evals before believing anything.
