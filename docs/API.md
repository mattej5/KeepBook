# API Contract

Pinned contract between backend (Andrew) and frontend (Vin). Change only by agreement — both sides build against this.

Base URL: `http://localhost:8100` (dev). All responses JSON.

> Port note (Sat ~1 AM): was 8000, moved to 8100 — Courier OS's bundled CourierDB binds `localhost:8000` whenever Courier runs, and both must coexist on the demo Mac. Backend binds 8100: `uvicorn main:app --port 8100`.

## Model call (backend internal)

All model access goes through one adapter — `backend/model_runtime.py`, exposing `extract(image_b64: str, prompt: str) -> str`. Nothing else in the backend may hardcode a model URL. Runtime picked by env:

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
