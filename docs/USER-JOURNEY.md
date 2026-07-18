# User Journey — A Day With KeepBook

Draft for team edit. This is the story spine for the pitch, the Writeup's Value section, and the demo narration. Time-savings figures are ILLUSTRATIVE (labeled), not cited statistics — we claim our own measured latencies, and frame human-time savings qualitatively.

## Persona

**Maria**, solo bookkeeper / enrolled agent. ~60 clients. Works from a home office; peak season is late January through April 15. Her clients send documents however they please: email attachments, texted phone photos, paper handed over at kitchen tables. She is the only reviewer, the only filer, and the only person liable.

Her two daily questions:
1. *What arrived, and are the numbers right?*
2. *What is each client still missing?*

## Before KeepBook — February 12th, the old way

- **7:45 AM.** Opens email. Nine attachments overnight from six clients — three clean PDFs, five phone photos (one sideways, one with a thumb in frame), one scan of a hand-annotated 1098. For each: download, open, squint, figure out what it is and whose it is, rename, drag to the client's folder, retype the numbers she needs into her workbook, update the "waiting on" spreadsheet. Call it 5–10 minutes per document when nothing goes wrong (ASSUMED, illustrative).
- **The quiet compliance failure.** Halfway through, she's tempted by the obvious shortcut: paste the W-2 into a chat AI and ask for the numbers. She knows what that means — a client's SSN and wages leaving her machine for a processor she has no agreement with. She does it anyway, or she doesn't and eats the hour. Both outcomes are bad. Nobody has given her a third option.
- **9:00 PM.** A client calls: "Did you get my K-1?" She doesn't know. The spreadsheet says one thing, the inbox says another. The scary version of this call comes in April, about a document that never arrived at all.

## With KeepBook — the same morning

- **7:45 AM — Intake (Capture/Submit screen).** She drags all nine attachments into the drop zone. "Processed on this Mac. Nothing is uploaded." She pours coffee. Gemma 4 works through the queue locally (~20s/doc measured on the demo M4 — VERIFIED); by the time she sits down, nine documents are classified, fields extracted, and grouped into per-client bins. The sideways photo still reads. The mystery attachment — a receipt someone sent by mistake — is sitting in **Unrecognized**, honestly unclassified instead of confidently wrong.
- **8:00 AM — Review (Bin Review screen).** The trust moment, and deliberately mandatory. Each document: source image on the left, extracted fields on the right. She confirms eight. On the ninth, the model read the wrong box — she strikes the wrong value (red pen), types the right one (ink blue), confirms. Fifteen seconds, and the mistake dies here instead of in a filed return. The software did the typing; **she stayed the authority.**
  - **Identity is a human gate, not a default.** Even when the model already guessed whose document this is, the client control starts *unconfirmed* — Maria affirmatively checks "This document belongs to [Name]" (or picks a different client) before Confirm unlocks. Misassignment is a confidentiality incident for a tax firm, so a silently-defaulted dropdown is not acceptable; assigning identity has to be an affirmative act. Continuation pages that carry no extractable name (page 2 of a K-1, the back of a 1098) get filed by hand under a client plus an optional **page number**, rendered as a "p. 2" label wherever the doc is listed.
- **8:10 AM — The day's marching orders (Checklist Dashboard).** Ruth Okafor's row inks itself complete. Marcus Whitfield: still missing a 1099-INT. The Chen partnership: K-1 and 1098 outstanding. The dashboard *is* the answer to "what is everyone still missing" — no spreadsheet archaeology. She sends two chase emails before 8:15, in February instead of April.
- **11:30 AM — Walk-in.** A client hands her a paper stack. Phone camera (stretch feature) or desk scanner → same queue, same bins, same checklist.
- **4:30 PM — Close of day.** One glance at the dashboard = tomorrow's chase list. The stats line reads: *41 fields extracted, 2 corrected.* That correction rate is her living accuracy meter — evidence the review step earns its keep, measured on her real documents, not our benchmark.

## What actually changed

| | Before | After |
|---|---|---|
| Sorting + retyping | Minutes of clerical triage per document (ASSUMED) | Seconds of review per document; the machine types, she judges |
| "What's missing?" | Reconstructed from inbox + memory + spreadsheet | Always-current per-client checklist |
| Wrong numbers | Found in April, or never | Caught at 8 AM in the review screen |
| Client data exposure | The 9 PM chat-tab temptation | Structurally impossible — no byte leaves the laptop |

The last row is the point: the alternative wasn't "worse AI," it was **no AI at all**, because Maria could never sign off on sending a client's identity to a third party. Local inference doesn't make the product private. It makes the product *possible*.

## Demo mapping (3 minutes) — hybrid: 30s story cold-open, then feature tour

- **0:00–0:30 — Cold open in Maria's shoes.** "It's February 12th. Nine attachments overnight. Her choice tonight is retype them by hand or paste a client's SSN into a chat tab with no data processing agreement." **Drop the folder live at ~0:20** so the queue processes behind the rest of the demo.
- **0:30–1:45 — Feature tour.** Capture screen ("Processed on this Mac. Nothing is uploaded."), bins filling as docs classify, then the review screen: confirm one clean extraction, catch the wrong one — red-pen strike, ink-blue correction — and show the misfit receipt sitting in Unrecognized instead of being force-fit.
- **1:45–2:20 — Checklist dashboard.** A client's row inks itself complete; another shows K-1 still missing. Stats line: fields extracted, fields corrected — the live correction-rate metric.
- **2:20–3:00 — Evidence + close.** Kill test in one line (e2b's confident wrong number vs e4b's 6/6), eval-harness numbers across the 26-doc test set, and the closer: "every byte stayed on this Mac."
- **Fallback:** a pre-processed session state stands ready if live processing stalls; rehearse both paths.
