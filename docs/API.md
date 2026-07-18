# API Contract

Pinned contract between backend (Andrew) and frontend (Vin). Change only by agreement — both sides build against this.

Base URL: `http://localhost:8000` (dev). All responses JSON.

## Model call (backend internal)

Ollama at `http://localhost:11434/api/generate`, model `gemma4:e4b`, `temperature: 0`, image as base64 in `images[]`. See `eval/run_test.py` for the working reference call. During dev on a non-Mac machine, set `OLLAMA_HOST` to Vin's Mac on the LAN.

## Data model

```jsonc
// Document
{
  "id": "doc_001",
  "client_id": "client_smith",        // null until assigned/binned
  "status": "pending" | "extracted" | "confirmed" | "unrecognized",
  "doc_type": "W-2" | "1099-NEC" | "1099-INT" | "1099-MISC" | "K-1" | "1098" | "UNRECOGNIZED",
  "image_path": "uploads/doc_001.png",
  "fields": {                          // extracted; keys vary by doc_type
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
| POST | `/intake` | multipart file(s) | `{"queued": ["doc_001", ...]}` | Accepts one or many images. Also support `{"folder": "/path"}` JSON body for folder-drop. |
| GET | `/queue` | — | `{"pending": n, "processing": "doc_002" \| null, "done": n}` | Frontend polls this during processing. |
| GET | `/documents` | — | `[Document, ...]` | Everything, all statuses. |
| GET | `/documents/{id}` | — | `Document` | |
| GET | `/documents/{id}/image` | — | image bytes | Review screen shows the source doc next to extracted fields. |
| POST | `/documents/{id}/confirm` | `{"client_id": "...", "doc_type": "...", "fields": {"box2_fed_withheld": "9,183.44", ...}}` | updated `Document` | Any field differing from extraction gets `corrected: true` + `original_value`. Sets status `confirmed`, updates client checklist. |
| GET | `/clients` | — | `[Client, ...]` | Dashboard source. |
| POST | `/clients` | `{"name": "...", "expected_docs": [...]}` | `Client` | Seed demo clients. |
| GET | `/stats` | — | `{"fields_extracted": n, "fields_corrected": n, "correction_rate": 0.04}` | The live-accuracy metric from PRD §9. Cheap to compute, big in demo. |

## Persistence

Single JSON file (`state.json`) written after every mutation. No DB. Restart-safe = demo-safe.

## Processing loop

Sequential queue, one doc at a time (e4b ~20s/doc on M4). Two model calls per doc are allowed if it helps: (1) classify doc_type, (2) type-specific field extraction prompt. Strict-JSON prompts, `temperature: 0`. On unparseable JSON: one retry, then mark `unrecognized`.
