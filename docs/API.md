# API Contract

Pinned contract between the backend and frontend lanes of Vin's build (both built by Vin + agents). Change only by agreement — both sides build against this.

Base URL: `http://localhost:8100` (dev). All responses JSON.

> Port note (Sat ~1 AM): was 8000, moved to 8100 — Courier OS's bundled CourierDB binds `localhost:8000` whenever Courier runs, and both must coexist on the demo Mac. Backend binds 8100: `uvicorn main:app --port 8100`.

## Model call (backend internal)

All model access goes through one adapter — `backend/model_runtime.py`, exposing `extract(image_b64: str, prompt: str) -> str` and `generate_text(prompt: str) -> str` (text-only, no image — used by the nudge-draft endpoint, see "Nudge draft" below). Nothing else in the backend may hardcode a model URL. Runtime picked by env:

| Env var | Default | Meaning |
|---|---|---|
| `MODEL_RUNTIME` | `ollama` | `ollama` or `courier` |
| `MODEL_NAME` | `gemma4:e4b` | model tag passed through to the runtime |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL — non-Mac dev machines point this at the model host over Tailscale (README "Models") |
| `COURIER_BASE_URL` | unset | OpenAI-compatible base URL for Courier OS — verified port is `http://localhost:9100/v1` |
| `COURIER_API_KEY` | unset | Courier requires `Authorization: Bearer <key>` on `/v1/*` (key comes from the account dashboard). Ollama needs no auth. |

Courier naming note (from docs research, Sat morning): Courier matches `model` case-insensitively against the workbench display name — `"Gemma 4 E4B"`, not `gemma4:e4b`. Either set `MODEL_NAME="Gemma 4 E4B"` when `MODEL_RUNTIME=courier`, or give the model a `gemma4:e4b` nickname in the Courier workbench. Its docs never show the standard `image_url` content-part shape — the first real image request (T41) confirms or kills the swap.

**ollama shape** (see `eval/run_test.py` for the verified reference call):
`POST {OLLAMA_HOST}/api/generate` with `{"model": MODEL_NAME, "prompt": prompt, "images": [image_b64], "stream": false, "options": {"temperature": 0}}` → read `response`.

**courier shape** (OpenAI-compatible chat completions):
`POST {COURIER_BASE_URL}/chat/completions` with `{"model": MODEL_NAME, "temperature": 0, "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}}]}]}` → read `choices[0].message.content`.

The eval runner (docs/EVAL.md) imports this same adapter and honors the same env vars, so eval numbers always measure the runtime that will actually demo.

## Data model

```jsonc
// Document
{
  "id": "doc_001",
  "client_id": "client_smith",        // null until assigned/binned
  "status": "pending" | "extracted" | "confirmed" | "unrecognized",
  // Extract types (fields extracted) + classify-only types (extract: false, T65)
  // + UNRECOGNIZED. Classify-only types land status "extracted" with fields:{}
  // — no extraction call runs — and are checked off once human-confirmed.
  "doc_type": "W-2" | "1099-NEC" | "1099-INT" | "1099-MISC" | "K-1" | "1098"
            | "1099-DIV" | "1099-B" | "1099-R" | "1099-G" | "1098-T" | "1098-E"
            | "1095-A" | "property tax statement" | "charitable receipt"
            | "brokerage statement" | "W-9" | "engagement letter" | "UNRECOGNIZED",
  "image_path": "uploads/doc_001.png",
  "received_at": "2026-07-18T09:14:02Z",   // OPTIONAL — set at intake; frontend shows "Received Jul 18" if present
  "phash": "a1b2…(64 hex chars)",         // OPTIONAL (additive) — 256-bit dHash (17x16 grid) as 64 hex chars, computed at intake. Absent on pre-feature docs; legacy 16-hex (64-bit era) values are lazily recomputed from the stored image at the next intake, or skipped if the image is gone.
  "duplicate_of": null,                    // OPTIONAL (additive) — doc_id this was flagged a near/exact duplicate of, or null. See "Duplicate detection".
  "source": "folder",                      // OPTIONAL (additive) — "folder" iff ingested by the watched-inbox thread; ABSENT for normal uploads (absent == "upload"). See "Watched intake folder".
  "fields": {                          // extracted; keys vary by doc_type
    // per-field OPTIONAL "low_confidence": true — backend sets it from honest deterministic signals
    // (model needed a retry, value empty, or format check failed e.g. SSN/EIN/TIN pattern, non-numeric money).
    // Frontend renders the highlighter flag only when present. No fake probability scores.
    "employee_name": {"value": "Marcus D. Whitfield", "corrected": false},
    "ssn": {"value": "412-55-9083", "corrected": false},
    "employer": {"value": "Cascade Logistics LLC", "corrected": false},
    "box1_wages": {"value": "68,420.15", "corrected": false},
    "box2_fed_withheld": {"value": "9,183.44", "corrected": true, "original_value": "70,110.00"}
  }
}

// Client
{
  "id": "client_smith",
  "name": "Smith, J.",
  "expected_docs": ["W-2", "1099-INT", "K-1"],   // the checklist
  "received_docs": ["W-2"]                        // confirmed only — extraction alone does NOT check an item off
}
```

Rule: a checklist item is satisfied only by a **confirmed** document. Unrecognized docs never force-fit into a type.

## Endpoints

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| POST | `/intake` | multipart file(s) | `{"queued": ["doc_001", ...]}` | Multipart field name is `file`, repeated once per file (pinned — the built frontend sends exactly this; FastAPI: `file: list[UploadFile] = File(...)`). Also support `{"folder": "/path"}` JSON body for folder-drop. |
| GET | `/queue` | — | `{"pending": n, "processing": "doc_002" \| null, "done": n}` | Frontend polls this during processing. |
| GET | `/documents` | — | `[Document, ...]` | Everything, all statuses. |
| GET | `/documents/{id}` | — | `Document` | |
| GET | `/documents/{id}/image` | — | image bytes | Review screen shows the source doc next to extracted fields. |
| POST | `/documents/{id}/confirm` | `{"client_id": "...", "doc_type": "...", "fields": {"box2_fed_withheld": "9,183.44", ...}}` | updated `Document` | Any field differing from extraction gets `corrected: true` + `original_value`. Sets status `confirmed`, updates client checklist. |
| POST | `/documents/{id}/resolve-duplicate` | `{"action": "keep"}` | updated `Document` | Clears `duplicate_of` (keep this copy). `404` unknown id; `400` if `action != "keep"`; no-op `200` if the doc carries no flag. To DISCARD the copy instead, use the existing `DELETE /documents/{id}`. See "Duplicate detection". |
| GET | `/clients` | — | `[Client, ...]` | Dashboard source. |
| POST | `/clients` | `{"name": "...", "expected_docs": [...]}` | `Client` | Seed demo clients. |
| GET | `/clients/{id}/export.csv` | — | `text/csv` (attachment) | Flat CSV of the client's **confirmed** docs, one row per field. `404` unknown client; header-only CSV when the client has no confirmed docs. See "CSV export" below. |
| GET | `/clients/{id}/nudge` | — | `{"client_id": "...", "missing": [...], "draft": str \| null, "generated_by": "model" \| "template"}` | Drafts a "still waiting on" reminder note from the client's missing checklist items. `404` unknown client. See "Nudge draft" below. |
| GET | `/stats` | — | `{"fields_extracted": n, "fields_corrected": n, "correction_rate": 0.04}` | The live-accuracy metric from PRD §9. Cheap to compute, big in demo. |
| GET | `/stats/timeline?hours=24` | — | see "Event log" below | **Stretch** — powers the Stats for Nerds screen. Build only after core endpoints are green. |

## CSV export

`GET /clients/{id}/export.csv` streams the client's **confirmed** documents as a
flat CSV — the hand-off surface. Response is `text/csv` with
`Content-Disposition: attachment; filename="{client_id}.csv"`. Escaping is stdlib
`csv` (values with commas/quotes/newlines are quoted). `404` for an unknown
client; a client with zero confirmed docs returns a valid header-only CSV.

**Grain: one row per field** (not per document). A W-2 with five fields is five
rows. Only `status == "confirmed"` documents are included — extraction alone
never exports. A corrected field exports its **corrected** value, with the value
it replaced in its own `original_value` column, so correction provenance
survives the hand-off.

**Field-less (classify-only) documents** (`extract: false`, e.g. `charitable
receipt`) carry no fields, so per-field grain would export them as zero rows and
drop them from the sheet. Instead each confirmed field-less doc emits exactly
one row with `field_key = "document"`, `field_label = "Document received"`, and
`value = doc_type` (`corrected = false`, empty `original_value`,
`low_confidence = false`). This keeps every confirmed document represented.

Columns (in order):

| column | meaning |
|---|---|
| `client_id` | the client's id |
| `client_name` | the client's display name |
| `doc_id` | source document id |
| `doc_type` | `W-2`, `1099-INT`, … |
| `received_at` | doc intake timestamp (may be empty) |
| `field_key` | schema key, e.g. `box2_fed_withheld` |
| `field_label` | human label (mirrors the UI's `FIELD_LABELS`; falls back to `field_key`) |
| `value` | the confirmed value (the corrected value when corrected) |
| `corrected` | `true` / `false` |
| `original_value` | the pre-correction value when `corrected`, else empty |
| `low_confidence` | `true` / `false` |

## Nudge draft (missing-doc reminder)

`GET /clients/{id}/nudge` — a visible-autonomy feature: the model **drafts** a
short per-client reminder note from the checklist gap; a human copies it and
sends it themselves. There is no send capability anywhere in KeepBook.

- Client with nothing missing → `200 {"client_id": ..., "missing": [], "draft": null}` (no `generated_by` key).
- Client with gaps → `200 {"client_id": ..., "missing": [...], "draft": "<text>", "generated_by": "model" | "template"}`.
- Unknown client → `404` (same error shape as the other client endpoints).

Draft path: the backend builds a strict prompt (greet the client by name, list
the exact missing document names verbatim, ask for them, nothing else — no
invented deadlines/amounts/fees, no `[placeholder]` text) and calls
`backend/model_runtime.py`'s `generate_text(prompt) -> str` (the same one
adapter `extract()` uses, just without an image). The model's raw output is
then post-checked deterministically:

- must contain the client's name
- must contain every missing document name, verbatim
- must not contain a `[` (placeholder tell)
- must be non-empty and ≤ 900 characters

Any check failure, or the call erroring/timing out, falls back to a
deterministic template with the same content guarantees (`generated_by:
"template"`) — the endpoint never 500s over a model misbehaving. Each call
appends one event to `backend/events.jsonl`:

```jsonc
{"ts": "2026-07-20T09:14:02Z", "type": "nudge_drafted", "client_id": "client_smith", "generated_by": "model"}
```

This event type is not one of `extracted` / `unrecognized` / `confirmed`, so
it is invisible to `GET /stats/timeline` — that endpoint's response shape is
unchanged.

## Duplicate detection (Phase 2, Tier A #1)

A client often submits the same document twice (emailed scan + phone photo, or a
literal double-drop). KeepBook notices at intake, flags it, and lets the human
resolve. **Model proposes, human confirms — a flagged copy is never auto-dropped.**
This also closes IMPROVEMENTS #14 (zero-byte/duplicate uploads accepted silently).

**Intake validation (400).** Every uploaded image is decoded at intake. A zero-byte
or PIL-unreadable upload returns `400` (`{"detail": "<name>: <reason>"}`, the same
shape as the other intake 400s) and **no document is created**. In a multi-file
batch, one bad file fails the whole request before any doc is written.

**Hashing.** For every accepted image the backend computes a sha256 (exact-byte
identity) and a 256-bit dHash (`phash`: grayscale → resize 17×16 → adjacent-pixel
compare, stored as 64 hex chars). PIL only, no new dependencies.

**Flagging (two-stage).** An exact sha256 match is a duplicate outright. Otherwise,
*stage 1* collects existing non-deleted docs within Hamming `THRESHOLD` of the new
dHash (nearest first) and *stage 2* confirms each candidate by pixel difference
against its stored image (both grayscale at 384², count pixels with |diff| > 60;
count ≤ 6 confirms). The first confirmed candidate becomes `duplicate_of`; the new
doc still runs the normal classify/extract pipeline; the existing doc is untouched.

> **Why two stages (`eval/dedup_calibration.py`, output committed alongside).**
> Measured at 64/256/576/1024-bit: on template-heavy forms the dHash collapses two
> *different* same-type docs (two people's W-2s) to distance 0 — the same band a
> re-encoded copy sits in — so no hash threshold can separate "duplicate copy"
> from "second client's same form". Pixel difference can: every realistic true-dup
> variant (PNG re-encode, JPEG ≥ q30, 0.5× downscale, resize round-trips, combos)
> measures **0** strongly-differing pixels, while same-type different-people pairs
> measure **14–411**. Shipped: stage-1 `THRESHOLD = 16` (true-dup hash band 0–10,
> different-type floor 37); stage-2 cutoff 6 (>2× under the distinct floor).
> Honest limits: (1) a real phone-photo of a scan sits far outside stage 1
> (~90+ bits) and is NOT caught — reliable catches are re-drops, re-encodes,
> recompressions, and moderate downscales; (2) extreme downscales (≤0.35×) alias
> the text past the stage-2 cutoff and are missed. Both are false *negatives* —
> by design the tradeoff never puts a false flag in front of the reviewer.

**Legacy phash migration.** State files written by the 64-bit scheme carry 16-hex
`phash` values. On the next intake comparison the backend recomputes those from the
doc's stored upload (persisted in place); a doc whose image is unreadable/gone is
skipped for comparison. A length mismatch is never compared directly — never a
crash, never a false flag.

**Resolving.** `POST /documents/{id}/resolve-duplicate {"action":"keep"}` clears
`duplicate_of` (sets null), persists, and appends a `dup_resolved` event. A doc
without a flag is an idempotent **no-op 200** (returns the doc unchanged, appends no
event) so a double-click or stale UI is safe. To DISCARD the extra copy instead,
use the existing `DELETE /documents/{id}` (no second delete path). Deleting a
document also clears any dangling `duplicate_of` on other docs that referenced it.

**Round-trip.** `phash`/`duplicate_of` are persisted in `state.json` and survive
restart. Old state files without the fields still load (additive/optional).

**Events** (appended to `events.jsonl`; additive — `/stats/timeline` ignores unknown
types and is unchanged):

```jsonc
{"ts": "2026-07-20T09:14:05Z", "type": "dup_flagged", "doc_id": "doc_008", "duplicate_of": "doc_007"}
{"ts": "2026-07-20T09:15:12Z", "type": "dup_resolved", "doc_id": "doc_008", "action": "keep"}
```

## Watched intake folder (Phase 2, Tier A #3)

Ambient autonomy: point KeepBook at a folder and any image dropped there (AirDrop,
Finder copy, a scanner's output dir) is ingested unprompted — no manual upload.

**Enabling.** Set the env var `KEEPBOOK_INBOX=/path/to/folder`. **Unset (default) =
feature fully off, zero behavior change.** Set = the folder is created at startup if
missing (parents ok) and a stdlib polling thread (~2 s interval, no new
dependencies, no `watchdog`/`fswatch`) watches it for the life of the process.
`GET /health` reports the resolved path as `"inbox"` (or `null` when off).

**Eligibility.** Top-level, non-hidden files with extension `png` / `jpg` / `jpeg`
/ `webp` (case-insensitive) only. This is intentionally narrower than the upload
set — `gif`/`bmp`, dotfiles, and subdirectories (no recursion) are ignored silently
(a log line only, never a document).

**Partial-write safety.** A file is ingested only once its `(size, mtime)` is
**stable across two consecutive polls**, so a file still being written (mid-AirDrop)
is left alone until it settles.

**Ingestion path.** Eligible, stable files run through the **same code path as
`POST /intake`** — the same zero-byte/undecodable rejection, dHash/sha256 hashing,
`duplicate_of` flagging, and queueing. The original file is **COPIED** into
`uploads/` and is **never moved, deleted, or renamed**; the watcher is strictly
read-and-copy. A doc created this way carries `"source": "folder"`.

**Re-ingest protection.** Handled files are remembered by **content sha256** in
`state.json` under the additive key `inbox_seen`. Consequences: a **restart** never
re-ingests; the **same bytes under a new filename** are skipped; a **new file that
reuses an old name but has different content** still ingests. A rejected
(zero-byte/undecodable) file is remembered too, so it is not retried forever — it is
logged, recorded, and skipped. Old state files without `inbox_seen` load fine
(defaults to empty).

**Resilience.** The scan loop is wrapped defensively — a permissions flip, a
vanished folder, or any per-file IO fault is logged and swallowed, so the watcher
never takes down the server.

**Events** (appended to `events.jsonl`; additive — `/stats/timeline` ignores unknown
types and is unchanged):

```jsonc
{"ts": "2026-07-20T09:14:05Z", "type": "inbox_ingested", "doc_id": "doc_009"}
{"ts": "2026-07-20T09:14:07Z", "type": "inbox_rejected", "name": "blank.png", "error": "empty upload (zero bytes)"}
```

## Event log (stretch tier — Stats for Nerds)

The backend appends one line per pipeline event to `backend/events.jsonl` (gitignored, append-only — this is the "stringent log"; the UI shows only a rolling window). Per extraction, the exact model I/O (every call's prompt + raw response, retries included) is written to `backend/raws/{doc_id}.json` (gitignored), referenced from the event as `raw_ref`:

```jsonc
{"ts": "2026-07-18T09:14:02Z", "type": "extracted", "doc_id": "doc_007", "doc_type": "W-2", "latency_s": 19.2, "fields_total": 5, "fields_low_confidence": 1, "retried": false, "raw_ref": "raws/doc_007.json"}
{"ts": "2026-07-18T09:15:40Z", "type": "confirmed", "doc_id": "doc_007", "doc_type": "W-2", "fields_corrected": 1, "corrected_keys": ["box2_fed_withheld"], "manual_type_change": false}
```

`GET /stats/timeline?hours=24` aggregates it:

```jsonc
{
  "hours": 24,
  "buckets": [{"hour": "09:00", "docs": 4, "corrections": 1}],   // one per hour, oldest first
  "totals": {
    "docs_processed": 31, "fields_extracted": 214,
    "fields_corrected": 9, "correction_rate": 0.042,
    "fields_low_confidence": 17,
    "first_try_type_acc": 0.94,          // confirmed doc_type == extracted doc_type
    "median_latency_s": 19.2,
    "p95_latency_s": 24.1,               // nearest-rank p95 over the same events
    "corrections_by_category": {"money": 4, "tin_ssn": 2, "names": 2, "doc_type": 1}
  }
}
```

Category mapping from field keys: `box*`/`*wages*`/`*income*`/`*comp*`/`*interest*` → money; `ssn`/`*tin*`/`*ein*` → tin_ssn; `*name*`/`payer`/`employer`/`lender`/`partnership*` → names; a confirm that changes `doc_type` → doc_type. Mockup reference: screen 3 in docs/design/tax-intake-mockup.html ("Stats for Nerds — live eval telemetry, rolling 24 hours"). Privacy line shown in UI: stats age out after 24h; nothing leaves the Mac.

## Persistence

Single JSON file (`state.json`) written after every mutation. No DB. Restart-safe = demo-safe. Event log is a separate append-only `backend/events.jsonl`.

## Processing loop

Sequential queue, one doc at a time (e4b ~20s/doc on M4). Two model calls per doc are allowed if it helps: (1) classify doc_type, (2) type-specific field extraction prompt. Strict-JSON prompts, `temperature: 0`. On unparseable JSON: one retry, then mark `unrecognized`. **Classify-only types (T65, `extract: false`) skip step (2) entirely — one model call, landing `extracted` with `fields:{}` for human confirm.**
