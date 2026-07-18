# Demo Script — 3:00 flat

Hybrid structure per docs/USER-JOURNEY.md: 30s story cold-open → feature tour → evidence → close. Verbatim lines in quotes; stage directions in brackets. Rehearse with a stopwatch (TASKS T50); cut lines from the middle, never the close.

## Pre-demo checklist (T53)

- [ ] Backend + frontend running; one warm inference completed < 5 min before slot
- [ ] Demo folder staged on Desktop: 6 docs — mix of clean + phone-photo variants + 1 receipt (the UNRECOGNIZED plant) + 1 doc known to produce a wrong field (the correction plant — pick from eval results)
- [ ] Seed state loaded: Ruth Okafor (one confirm from complete), Marcus Whitfield (missing 1099-INT), Chen partnership (missing K-1 + 1098)
- [ ] Fallback restore command sitting in a ready terminal (T43)
- [ ] Wi-Fi OFF if T40 passed with it off — say so out loud in the demo

## Script

**0:00 — Cold open** [checklist dashboard on screen, but don't explain it yet]

> "It's February 12th. Maria does the books for sixty clients, and nine tax documents landed in her inbox overnight. Tonight she has two options: retype every number by hand, or paste a client's Social Security number into some chat tab — a cloud AI she has no data processing agreement with. Most bookkeepers quietly pick option two. That's the problem."

**0:20** [drag the demo folder into the drop zone — processing starts NOW and runs behind the rest of the demo]

> "So we built KeepBook. I just dropped her morning inbox in. Everything you're about to see — the model included — is running on this Mac. [if T40 passed:] The Wi-Fi is off."

**0:35 — Capture screen** [point at the on-device banner, then the queue filling]

> "Gemma 4 — the e4b model, running locally through [Ollama / Courier OS — whichever passed T41] — is classifying each document, extracting the fields, and sorting them into per-client bins. About twenty seconds a document, so they'll keep landing while we talk."

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

> "We didn't guess the trust model — we measured it. Same synthetic W-2 to Gemma's two smallest on-device sizes: the 2-billion model returned a wrong dollar amount for tax withheld — clean JSON, confident, indistinguishable from right. The 4-billion model got six out of six. So we built a 29-document eval — clean scans, phone photos, junk documents, and real handwriting: [doc-type accuracy], [field accuracy], and the silent-wrong-value count is the number we watch."

**The SSN story** [tell this one with relish — it's true and it's the whole product in 20 seconds]:

> "My favorite result from last night: at 3 AM, my wife hand-filled tax forms with a stylus so we could test real handwriting. When we imported them, we noted one risk: she writes her 3s with a flat top — easy to mistake for a 5. Hours later the model read her handwritten SSN... and made *exactly* that mistake. 457 instead of 437. Perfect JSON. Wrong identity. No error, no warning — unless you have a review screen. We predicted the failure, the model walked into it, and the red pen caught it. That's why the human gate isn't a fallback in this product. It's the product."

**2:45 — Close**

> "Small firms handling strangers' SSNs were never going to get a data processing agreement from a chat tab. KeepBook doesn't make cloud AI safer — it removes the cloud entirely. Every byte of every client's tax life stayed on this MacBook. That's KeepBook. Thanks."

**If live processing stalls:** run the fallback restore [T43], say "let me jump to a session from this morning," continue at 1:00. Do not debug on stage.

---

# Judge Q&A crib

**"Why not just OCR — Tesseract, or AWS Textract?"**
Textract and Document AI are cloud — the exact DPA problem we exist to remove. Local OCR gives you characters, not answers: it won't classify a form, map "Box 2" to federal withholding, or survive a skewed phone photo with a shadow. Gemma reads the document the way a person does — layout, labels, context — and returns structured fields.

**"How do you know it's accurate? What about hallucination?"**
We assume it isn't, and built the product around that. Our own kill test caught the small model hallucinating a dollar figure in perfect JSON. Three answers stacked: (1) the eval harness — [N] labeled docs, scored per-field, silent-wrong values tracked as their own class; (2) mandatory human review — nothing enters the checklist unconfirmed; (3) the correction rate — a live accuracy metric on the user's real documents, forever.

**"Why e4b? Why not the smallest model, or the biggest?"**
We ship the smallest model that passed our kill test. e2b failed it — silently wrong money value. e4b passed 6/6 at ~20s/doc on this hardware. We didn't ship 12b because we have no evidence it buys accuracy we need, and it costs latency we'd feel.

**"What about handwriting / really messy documents?"**
Tested honestly with real pen-filled forms: the model reads handwriting at [58%] of fields — and its misses look like a careless human's ("Celesle" for "Celeste", a flat-topped 3 read as 5). Our policy: handwritten documents are *always* flagged for full review, no exceptions — but even so, confirming a pre-filled field takes a couple of seconds while typing it takes fifteen. The model is the typist; the reviewer stays the accountant. [If the ensemble experiment landed: we also cross-check handwriting with two model sizes — agreement raises confidence, disagreement raises the flag — but the human gate stays regardless.]

**"Isn't 60-something percent extraction bad?"**
It would be, if we hid it. Instead it's the design input: extraction is fallible, so review is mandatory, corrections are one click, and the correction rate is on a screen. The economics still work — verifying a filled field is ~5x faster than retyping it, so even correcting a third of fields, the bookkeeper finishes far ahead of manual entry. And the alternative wasn't better AI, it was no AI at all.

**"Who pays for this?"**
The firm — per-seat, seasonal peak. The buyer is the person liable for the data: solo bookkeepers, small CPA firms. They can't use cloud AI at any price, so the alternative isn't a cheaper competitor — it's retyping.

**"Isn't this just a wrapper around Gemma?"**
Gemma is the reader. The product is what surrounds it: binning, the per-client checklist, mandatory review with correction provenance (struck value preserved), the Unrecognized path, and an eval harness that measures the whole pipeline. Swap the model next year; the product remains.

**"Does it file taxes?"**
No, and deliberately never claims to. It organizes and verifies intake for a human preparer. Filing is licensed territory — we stay on the safe side of that line (PRD §5).

**"Why Gemma 4 specifically?"**
Vision-capable at sizes that genuinely fit on-device — e4b is 8B params quantized, ~9.6GB, comfortable in 24GB unified memory with room for the OS. Native vision means no OCR pipeline. And the on-device requirement isn't a track constraint we tolerated — it's the product's entire reason to exist.

**"What broke at 2 AM?"** [have a real answer ready — fill in tomorrow]
[TODO: fill with the true story of the night's worst bug — judges love this question and honesty plays well.]
