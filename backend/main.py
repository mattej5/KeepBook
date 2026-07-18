"""KeepBook backend — FastAPI, on-device tax-document intake.

Implements docs/API.md exactly, on port 8100. All model access goes through
backend/model_runtime.py via backend/pipeline.py. State is a single JSON file
(state.json) rewritten after every mutation, so restart == demo-safe.

Run:  uvicorn main:app --port 8100      (from backend/, venv active)
"""

import base64
import copy
import csv
import io
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile

import pipeline

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Real source dir, captured once. Tests monkeypatch BASE_DIR to a tmp dir; the
# git-sha stamp must still resolve against the actual repo, so keep it separate.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
EVENTS_PATH = os.path.join(BASE_DIR, "events.jsonl")
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))

os.makedirs(UPLOADS_DIR, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# ---------------------------------------------------------------------------
# In-memory state, guarded by a single lock. Serialized to STATE_PATH.
# documents/clients are dicts keyed by id (insertion order preserved).
# queue/processing are runtime-only (rebuilt from status on load).
# ---------------------------------------------------------------------------
STATE_LOCK = threading.RLock()
EVENTS_LOCK = threading.Lock()
WAKE = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(event: dict) -> None:
    """Append one pipeline event to events.jsonl (docs/API.md "Event log")."""
    row = {"ts": _now_iso(), **event}
    line = json.dumps(row) + "\n"
    with EVENTS_LOCK:
        with open(EVENTS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)

STATE = {
    "documents": {},   # id -> Document
    "clients": {},     # id -> Client
    "seq_doc": 0,
    "seq_client": 0,
}
QUEUE = []             # pending doc ids awaiting processing
PROCESSING = None      # id currently being processed, or None

# Config stamp for /health — resolved once at startup (docs/IMPROVEMENTS.md #12).
STARTED_AT = None
GIT_SHA = None


def _git_sha() -> str:
    """Short HEAD sha of the source repo, or 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_SRC_DIR,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - health must never fail on a git hiccup
        return "unknown"


def _reset_processing() -> None:
    """Clear PROCESSING under the lock. Used by the worker's failure guard."""
    global PROCESSING
    with STATE_LOCK:
        PROCESSING = None


def _persist_locked() -> None:
    """Write STATE atomically. Caller must hold STATE_LOCK."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(STATE, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _load_state() -> None:
    """Load STATE from disk and rebuild the processing queue from status."""
    global QUEUE, PROCESSING
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        STATE["documents"] = data.get("documents", {})
        STATE["clients"] = data.get("clients", {})
        STATE["seq_doc"] = data.get("seq_doc", 0)
        STATE["seq_client"] = data.get("seq_client", 0)
    # Any document still "pending" was never finished -> re-enqueue.
    QUEUE = [
        doc_id
        for doc_id, doc in STATE["documents"].items()
        if doc.get("status") == "pending"
    ]
    PROCESSING = None
    if QUEUE:
        WAKE.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _next_doc_id() -> str:
    STATE["seq_doc"] += 1
    return f"doc_{STATE['seq_doc']:03d}"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "client"


def _next_client_id(name: str) -> str:
    base = "client_" + _slugify(name)
    cid = base
    n = 2
    while cid in STATE["clients"]:
        cid = f"{base}_{n}"
        n += 1
    return cid


def _abs_image_path(doc: dict) -> str:
    ip = doc.get("image_path")
    if not ip:
        return ""
    return ip if os.path.isabs(ip) else os.path.join(BASE_DIR, ip)


def _wrap_fields(plain: dict, retried: bool = False) -> dict:
    """Turn {key: value_str} into the stored {key: {value, corrected}} shape.

    Adds "low_confidence": true only when a deterministic signal fires
    (docs/API.md) — never a fake probability, and only present when true.
    """
    out = {}
    for k, v in plain.items():
        field = {"value": v, "corrected": False}
        if pipeline.field_low_confidence(k, v, retried):
            field["low_confidence"] = True
        out[k] = field
    return out


def _write_raws(
    doc_id: str, calls: list, doc: dict, latency: float = None, retried: bool = False
) -> str:
    """Persist the exact per-call model I/O for one document (auditability).

    Returns the event-facing reference path (relative to backend/).
    """
    raws_dir = os.path.join(BASE_DIR, "raws")
    os.makedirs(raws_dir, exist_ok=True)
    rel = os.path.join("raws", f"{doc_id}.json")
    record = {
        "doc_id": doc_id,
        "ts": _now_iso(),
        "model_runtime": os.environ.get("MODEL_RUNTIME", "ollama"),
        "model_name": os.environ.get("MODEL_NAME", "gemma4:e4b"),
        "status": doc.get("status"),
        "doc_type": doc.get("doc_type"),
        "latency_s": latency,
        "retried": retried,
        "calls": calls,
    }
    with open(os.path.join(BASE_DIR, rel), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return rel


# ---------------------------------------------------------------------------
# Background worker — sequential, one document at a time.
# ---------------------------------------------------------------------------
def _worker_loop() -> None:
    """Drain the queue forever. Each step is guarded so a persist/IO failure
    records a worker_error event instead of killing the daemon (IMPROVEMENTS #9)."""
    while True:
        WAKE.wait(timeout=1.0)
        try:
            _worker_step()
        except Exception as exc:  # noqa: BLE001 - the worker must never die
            _reset_processing()
            try:
                _append_event({"type": "worker_error", "error": str(exc)})
            except Exception:  # noqa: BLE001 - best-effort signal only
                pass


def _worker_step() -> None:
    global PROCESSING
    with STATE_LOCK:
        if not QUEUE:
            WAKE.clear()
            doc_id = None
        else:
            doc_id = QUEUE.pop(0)
            PROCESSING = doc_id
            doc = STATE["documents"].get(doc_id)
            img_path = _abs_image_path(doc) if doc else ""
    if doc_id is None:
        return

    result = None
    error = None
    t0 = time.time()
    # Raw-I/O capture: wrap the pipeline's bound model hook for the duration
    # of this one document so every call's exact prompt + raw response is
    # recorded (incl. retries). Worker is the only sequential model caller;
    # the original binding (real adapter or a test fake) is restored in
    # finally BEFORE the doc reaches a terminal status.
    calls = []
    orig_extract = pipeline.model_extract

    def _capturing_extract(image_b64, prompt, *args, **kwargs):
        # Stage label for this call. There are no tool calls and no chain-of-
        # thought here, so the truthful trace unit is the pipeline STAGE that
        # issued the call; the pipeline publishes it in pipeline._capture_stage
        # right before calling. Snapshot it now (synchronous, single worker).
        cap = getattr(pipeline, "_capture_stage", None) or {}
        stage = cap.get("stage")
        retry_call = bool(cap.get("retry"))
        entry = {"seq": len(calls) + 1, "prompt": prompt}
        if stage is not None:
            entry["stage"] = stage
        if retry_call:
            entry["retry"] = True
        try:
            resp = orig_extract(image_b64, prompt, *args, **kwargs)
        except Exception as exc:
            entry["error"] = str(exc)
            calls.append(entry)
            raise
        entry["response"] = resp
        calls.append(entry)
        return resp

    pipeline.model_extract = _capturing_extract
    try:
        with open(img_path, "rb") as fh:
            img_b64 = base64.b64encode(fh.read()).decode()
        result = pipeline.run_pipeline(img_b64)
    except Exception as exc:  # noqa: BLE001 - never let the worker die
        error = str(exc)
    finally:
        pipeline.model_extract = orig_extract
    latency = round(time.time() - t0, 2)

    # Compute the terminal values, PERSIST THE RAW TRACE, and only then flip the
    # observable status. A reader keying off status (the /trace endpoint) must
    # never see "extracted" before the matching raws/<id>.json is on disk —
    # otherwise it can serve a stale trace from an earlier document.
    retried = bool(result.get("retried")) if result else False
    if result is None:
        # A raised model/IO call (unreachable model, timeout) is an OUTAGE, not
        # honest confusion — distinct "error" status + machine-readable error,
        # so the UI shows the red banner, never the "unrecognized" copy
        # (IMPROVEMENTS #1).
        new_status, new_doc_type, new_fields, new_error = (
            "error",
            pipeline.UNRECOGNIZED,
            {},
            error,
        )
    else:
        new_status = result["status"]
        new_doc_type = result["doc_type"]
        new_fields = _wrap_fields(result["fields"], result.get("retried"))
        new_error = None

    try:
        raw_ref = _write_raws(
            doc_id,
            calls,
            {"status": new_status, "doc_type": new_doc_type},
            latency,
            retried,
        )
    except OSError:
        raw_ref = None

    low_conf_count = 0
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is not None:
            doc["status"] = new_status
            doc["doc_type"] = new_doc_type
            doc["fields"] = new_fields
            if new_error is not None:
                doc["error"] = new_error
            else:
                doc.pop("error", None)
            low_conf_count = sum(
                1 for f in doc["fields"].values() if f.get("low_confidence")
            )
        PROCESSING = None
        _persist_locked()

    # Event log (append-only; drives /stats/timeline).
    if doc is not None:
        fields_total = len(result["fields"]) if result else 0
        ev_type = (
            new_status
            if new_status in ("extracted", "unrecognized", "error")
            else "extracted"
        )
        event = {
            "type": ev_type,
            "doc_id": doc_id,
            "doc_type": new_doc_type,
            "latency_s": latency,
            "fields_total": fields_total,
            "fields_low_confidence": low_conf_count,
            "retried": retried,
            "re_asks": int(result.get("re_asks", 0)) if result else 0,
            "preprocessed": pipeline._preprocess_enabled(),
            "model": os.environ.get("MODEL_NAME", "gemma4:e4b"),
            "raw_ref": raw_ref,
        }
        if new_error is not None:
            event["error"] = new_error
        _append_event(event)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="KeepBook", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    global STARTED_AT, GIT_SHA
    STARTED_AT = _now_iso()
    GIT_SHA = _git_sha()
    with STATE_LOCK:
        _load_state()
    t = threading.Thread(target=_worker_loop, name="keepbook-worker", daemon=True)
    t.start()


@app.get("/health")
async def health():
    """Config stamp — one curl proves which code/model a process is serving."""
    return {
        "status": "ok",
        "model_runtime": os.environ.get("MODEL_RUNTIME", "ollama"),
        "model_name": os.environ.get("MODEL_NAME", "gemma4:e4b"),
        "preprocess": pipeline._preprocess_enabled(),
        "git_sha": GIT_SHA,
        "started_at": STARTED_AT,
    }


# ---------------------------- Intake ---------------------------------------
async def _save_bytes(data: bytes, orig_name: str) -> dict:
    """Create a pending Document from raw image bytes. Caller holds lock."""
    ext = os.path.splitext(orig_name or "")[1].lower()
    if ext not in IMAGE_EXTS:
        ext = ".png"
    doc_id = _next_doc_id()
    rel_path = os.path.join("uploads", f"{doc_id}{ext}")
    with open(os.path.join(BASE_DIR, rel_path), "wb") as fh:
        fh.write(data)
    doc = {
        "id": doc_id,
        "client_id": None,
        "status": "pending",
        "doc_type": None,
        "image_path": rel_path,
        "received_at": _now_iso(),
        "fields": {},
        "source_name": orig_name or None,
    }
    STATE["documents"][doc_id] = doc
    QUEUE.append(doc_id)
    return doc


@app.post("/intake")
async def intake(request: Request):
    ct = request.headers.get("content-type", "")
    queued = []

    if ct.startswith("application/json"):
        body = await request.json()
        folder = body.get("folder")
        if not folder or not os.path.isdir(folder):
            raise HTTPException(400, f"folder not found: {folder!r}")
        names = sorted(
            n
            for n in os.listdir(folder)
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS
        )
        if not names:
            raise HTTPException(400, f"no images in folder: {folder!r}")
        with STATE_LOCK:
            for n in names:
                with open(os.path.join(folder, n), "rb") as fh:
                    data = fh.read()
                doc = await _save_bytes(data, n)
                queued.append(doc["id"])
            _persist_locked()
    else:
        form = await request.form()
        # multi_items() keeps EVERY (key, value) pair — the built frontend sends
        # each file under a repeated "file" key (frontend/js/api.js), and
        # form.values() would collapse them to one. Field name pinned to "file"
        # per docs/API.md; other keys are rejected, not silently accepted.
        uploads = [
            v
            for k, v in form.multi_items()
            if k == "file" and isinstance(v, StarletteUploadFile)
        ]
        if not uploads:
            raise HTTPException(400, 'no files under multipart field "file"')
        # Images only — KeepBook renders no PDFs (IMPROVEMENTS #6). Reject a
        # non-image upload loudly instead of silently coercing it to .png and
        # producing a fake "unrecognized".
        for up in uploads:
            ext = os.path.splitext(up.filename or "")[1].lower()
            if ext not in IMAGE_EXTS:
                raise HTTPException(
                    400,
                    f"unsupported file type {ext or '(none)'!r} for {up.filename!r}; "
                    "images only (png, jpg, jpeg, webp, gif, bmp)",
                )
        with STATE_LOCK:
            for up in uploads:
                data = await up.read()
                doc = await _save_bytes(data, up.filename)
                queued.append(doc["id"])
            _persist_locked()

    WAKE.set()
    return {"queued": queued}


# ----------------------------- Queue ---------------------------------------
@app.get("/queue")
async def get_queue():
    with STATE_LOCK:
        # Count docs still "pending", NOT len(QUEUE): the in-flight document is
        # popped off QUEUE while it is being read, yet it is still pending until
        # the worker writes a terminal status. Counting status keeps that
        # in-flight doc visible so completion isn't declared a doc early
        # (IMPROVEMENTS #3).
        pending = sum(
            1 for d in STATE["documents"].values() if d.get("status") == "pending"
        )
        processing = PROCESSING
        done = sum(
            1
            for d in STATE["documents"].values()
            if d.get("status") in ("extracted", "unrecognized", "confirmed", "error")
        )
    return {"pending": pending, "processing": processing, "done": done}


# --------------------------- Documents -------------------------------------
@app.get("/documents")
async def get_documents():
    # Deep-copy under the lock: the returned docs are serialized by Starlette
    # AFTER this function exits, so handing out live dict references races the
    # worker mid-mutation (IMPROVEMENTS #10).
    with STATE_LOCK:
        return copy.deepcopy(list(STATE["documents"].values()))


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        return copy.deepcopy(doc)


@app.get("/documents/{doc_id}/trace")
async def get_document_trace(doc_id: str):
    """Serve the exact per-call model I/O captured for one document.

    The observability money-shot: one click answers "what did the model see
    and say?" (IMPROVEMENTS #4). Returns the raws/<id>.json record verbatim.
    """
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
    raws_path = os.path.join(BASE_DIR, "raws", f"{doc_id}.json")
    if not os.path.exists(raws_path):
        raise HTTPException(404, f"no trace for {doc_id}")
    with open(raws_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/documents/{doc_id}/image")
async def get_document_image(doc_id: str):
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        path = _abs_image_path(doc)
    if not path or not os.path.exists(path):
        raise HTTPException(404, f"no image for {doc_id}")
    return FileResponse(path)


@app.post("/documents/{doc_id}/confirm")
async def confirm_document(doc_id: str, request: Request):
    body = await request.json()
    incoming = body.get("fields", {}) or {}

    # Identity is the human gate. Misassigning a document to the wrong client is
    # a confidentiality incident for a tax firm, so client identity must be an
    # affirmative act by the reviewer — never a silently-defaulted dropdown. The
    # affirmative "This document belongs to X" step is enforced in the Review UI
    # (the identity control starts UNCONFIRMED even when the pipeline pre-assigned
    # a client); the pinned /confirm contract still accepts client_id: null, so
    # enforcement lives in the frontend rather than here. page_number lets
    # continuation pages that carry no extractable name be filed by hand under a
    # client + page number. Additive: omitting it preserves the existing contract.
    page_number = body.get("page_number")
    if page_number is not None:
        # Accept an int or an integer-valued string/float (the frontend number
        # input surfaces its value as a string); reject everything else — a
        # truncated "1.5" or a stray "true" must fail loudly, not file silently.
        valid = False
        if isinstance(page_number, bool):
            valid = False
        elif isinstance(page_number, int):
            valid = page_number >= 1
        elif isinstance(page_number, float):
            valid = page_number.is_integer() and page_number >= 1
            if valid:
                page_number = int(page_number)
        elif isinstance(page_number, str) and page_number.strip().isdigit():
            page_number = int(page_number.strip())
            valid = page_number >= 1
        if not valid:
            raise HTTPException(400, "page_number must be an integer >= 1")

    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")

        old_type = doc.get("doc_type")
        manual_type_change = bool(body.get("doc_type")) and body["doc_type"] != old_type
        if body.get("doc_type"):
            doc["doc_type"] = body["doc_type"]
        if body.get("client_id") is not None:
            doc["client_id"] = body["client_id"]
        if page_number is not None:
            doc["page_number"] = page_number

        fields = doc.get("fields", {})
        corrected_keys = []
        for key, raw_val in incoming.items():
            new_val = "" if raw_val is None else str(raw_val)
            cur = fields.get(key, {"value": "", "corrected": False})
            baseline = cur.get("original_value") if cur.get("corrected") else cur.get("value", "")
            if new_val != str(cur.get("value", "")):
                fields[key] = {
                    "value": new_val,
                    "corrected": True,
                    "original_value": baseline,
                }
                # A field the reviewer corrected is no longer low-confidence.
                fields[key].pop("low_confidence", None)
                corrected_keys.append(key)
        doc["fields"] = fields
        doc["status"] = "confirmed"

        # Checklist: a confirmed doc_type joins the client's received_docs.
        client = STATE["clients"].get(doc.get("client_id"))
        if client is not None:
            dt = doc.get("doc_type")
            if dt and dt != pipeline.UNRECOGNIZED and dt not in client["received_docs"]:
                client["received_docs"].append(dt)

        _persist_locked()
        final = json.loads(json.dumps(doc))  # snapshot for use outside the lock

    _append_event({
        "type": "confirmed",
        "doc_id": doc_id,
        "doc_type": final.get("doc_type"),
        "fields_corrected": len(corrected_keys),
        "corrected_keys": corrected_keys,
        "manual_type_change": manual_type_change,
    })
    return final


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Remove a document (an erroneous ingest) from state.

    Count-aware checklist un-check: a confirmed doc_type is only pulled from the
    client's received_docs when NO other confirmed doc of that type remains for
    the client. Persists under the same lock + atomic _persist_locked pattern as
    /confirm; logs a "deleted" event; best-effort deletes the uploaded image and
    the raws/<id>.json trace (never fails the request over IO).
    """
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")

        was_confirmed = doc.get("status") == "confirmed"
        client_id = doc.get("client_id")
        doc_type = doc.get("doc_type")
        image_abs = _abs_image_path(doc)

        del STATE["documents"][doc_id]
        if doc_id in QUEUE:
            QUEUE.remove(doc_id)

        # Un-check the client's checklist item only if this was the last confirmed
        # document of its type for that client (count-aware).
        if (
            was_confirmed
            and client_id
            and doc_type
            and doc_type != pipeline.UNRECOGNIZED
        ):
            client = STATE["clients"].get(client_id)
            if client is not None and doc_type in client.get("received_docs", []):
                still_have = any(
                    d.get("client_id") == client_id
                    and d.get("doc_type") == doc_type
                    and d.get("status") == "confirmed"
                    for d in STATE["documents"].values()
                )
                if not still_have:
                    client["received_docs"] = [
                        t for t in client["received_docs"] if t != doc_type
                    ]

        _persist_locked()

    # Best-effort cleanup of on-disk artifacts — never fail the request over IO.
    for path in (image_abs, os.path.join(BASE_DIR, "raws", f"{doc_id}.json")):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    _append_event({"type": "deleted", "doc_id": doc_id, "doc_type": doc_type})
    return {"deleted": doc_id}


# ---------------------------- Clients --------------------------------------
@app.get("/clients")
async def get_clients():
    with STATE_LOCK:
        return list(STATE["clients"].values())


@app.post("/clients")
async def create_client(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(400, "client name required")
    expected = body.get("expected_docs", []) or []
    with STATE_LOCK:
        cid = _next_client_id(name)
        client = {
            "id": cid,
            "name": name,
            "expected_docs": list(expected),
            "received_docs": [],
        }
        STATE["clients"][cid] = client
        STATE["seq_client"] += 1
        _persist_locked()
        return client


# Human-readable field labels for the CSV export's field_label column. Mirrors
# frontend/js/app.js FIELD_LABELS so an exported sheet reads the same as the UI.
# Unknown keys fall back to the raw snake_case key (see _field_label).
_CSV_FIELD_LABELS = {
    "employer": "Employer", "ein": "EIN", "employee_name": "Employee", "ssn": "SSN",
    "box1_wages": "Wages (Box 1)", "box2_fed_withheld": "Fed. tax withheld (Box 2)",
    "payer": "Payer", "recipient": "Recipient", "recipient_tin": "Recipient TIN",
    "box1_interest": "Interest income (Box 1)", "box4_fed_withheld": "Fed. tax withheld (Box 4)",
    "box1_nonemployee_comp": "Nonemployee comp. (Box 1)",
    "lender": "Lender", "borrower": "Borrower", "borrower_tin": "Borrower TIN",
    "box1_mortgage_interest": "Mortgage interest (Box 1)",
    "recipient_name": "Recipient", "box1_interest_income": "Interest income (Box 1)",
    "box3_other_income": "Other income (Box 3)",
    "partnership_name": "Partnership", "partner_name": "Partner",
    "partnership_ein": "Partnership EIN", "ordinary_income": "Ordinary income",
    "borrower_name": "Borrower",
}

_CSV_COLUMNS = [
    "client_id", "client_name", "doc_id", "doc_type", "received_at",
    "field_key", "field_label", "value", "corrected", "original_value",
    "low_confidence",
]


def _field_label(key: str) -> str:
    return _CSV_FIELD_LABELS.get(key, key)


@app.get("/clients/{client_id}/export.csv")
async def export_client_csv(client_id: str):
    """Flat CSV of one client's CONFIRMED documents — one row per field.

    Integration surface: anything that imports CSV (spreadsheets, ledgers, tax
    prep) can read this today. Only confirmed docs are exported; a corrected
    field ships its corrected value plus the original_value it replaced, so the
    correction provenance survives the hand-off. See docs/API.md "CSV export".
    """
    with STATE_LOCK:
        client = STATE["clients"].get(client_id)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")
        client_name = client.get("name", "")
        rows = []
        for doc in STATE["documents"].values():
            if doc.get("client_id") != client_id or doc.get("status") != "confirmed":
                continue
            doc_id = doc.get("id", "")
            doc_type = doc.get("doc_type", "")
            received_at = doc.get("received_at", "")
            for key, field in (doc.get("fields") or {}).items():
                corrected = bool(field.get("corrected"))
                rows.append([
                    client_id,
                    client_name,
                    doc_id,
                    doc_type,
                    received_at,
                    key,
                    _field_label(key),
                    field.get("value", ""),
                    "true" if corrected else "false",
                    field.get("original_value", "") if corrected else "",
                    "true" if field.get("low_confidence") else "false",
                ])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{client_id}.csv"'},
    )


# ----------------------------- Stats ---------------------------------------
@app.get("/stats")
async def get_stats():
    with STATE_LOCK:
        extracted = 0
        corrected = 0
        for d in STATE["documents"].values():
            if d.get("status") == "unrecognized":
                continue
            for f in (d.get("fields") or {}).values():
                extracted += 1
                if f.get("corrected"):
                    corrected += 1
    rate = round(corrected / extracted, 4) if extracted else 0
    return {
        "fields_extracted": extracted,
        "fields_corrected": corrected,
        "correction_rate": rate,
    }


# ---------------------- Timeline (stretch: Stats for Nerds) ----------------
def _read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    events = []
    with EVENTS_LOCK:
        with open(EVENTS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return events


def _parse_ts(ts: str):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


@app.get("/stats/timeline")
async def stats_timeline(hours: int = 24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = []
    for e in _read_events():
        dt = _parse_ts(e.get("ts", ""))
        if dt is not None and dt >= cutoff:
            e["_dt"] = dt
            events.append(e)

    processed = [e for e in events if e.get("type") in ("extracted", "unrecognized")]
    confirms = [e for e in events if e.get("type") == "confirmed"]

    # Hourly buckets: exactly `hours` zero-filled entries, one per hour, oldest
    # first, ending at the current hour (contract "one per hour, oldest first").
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_hour = now_hour - timedelta(hours=hours - 1)
    counts = {}  # absolute hour datetime -> {"docs": n, "corrections": n}
    for e in processed:
        key = e["_dt"].replace(minute=0, second=0, microsecond=0)
        counts.setdefault(key, {"docs": 0, "corrections": 0})["docs"] += 1
    for e in confirms:
        key = e["_dt"].replace(minute=0, second=0, microsecond=0)
        counts.setdefault(key, {"docs": 0, "corrections": 0})["corrections"] += int(
            e.get("fields_corrected", 0)
        )
    bucket_list = []
    for offset in range(hours):
        hour_dt = start_hour + timedelta(hours=offset)
        c = counts.get(hour_dt, {"docs": 0, "corrections": 0})
        bucket_list.append({
            "hour": hour_dt.strftime("%H:00"),
            "docs": c["docs"],
            "corrections": c["corrections"],
        })

    docs_processed = len(processed)
    fields_extracted = sum(int(e.get("fields_total", 0)) for e in processed)
    fields_low_confidence = sum(int(e.get("fields_low_confidence", 0)) for e in processed)
    fields_corrected = sum(int(e.get("fields_corrected", 0)) for e in confirms)
    manual_changes = sum(1 for e in confirms if e.get("manual_type_change"))
    latencies = sorted(
        float(e["latency_s"]) for e in processed if e.get("latency_s") is not None
    )

    if latencies:
        mid = len(latencies) // 2
        median_lat = (
            latencies[mid]
            if len(latencies) % 2
            else (latencies[mid - 1] + latencies[mid]) / 2
        )
        # Nearest-rank p95 over the same sorted latencies.
        p95_lat = latencies[min(len(latencies) - 1, math.ceil(0.95 * len(latencies)) - 1)]
    else:
        median_lat = 0
        p95_lat = 0

    # All four categories always present, zero-filled (contract).
    by_cat = {"money": 0, "tin_ssn": 0, "names": 0, "doc_type": 0}
    for e in confirms:
        for key in e.get("corrected_keys", []) or []:
            cat = pipeline.correction_category(key)
            by_cat[cat] = by_cat.get(cat, 0) + 1
        if e.get("manual_type_change"):
            by_cat["doc_type"] += 1

    totals = {
        "docs_processed": docs_processed,
        "fields_extracted": fields_extracted,
        "fields_low_confidence": fields_low_confidence,
        "fields_corrected": fields_corrected,
        "correction_rate": round(fields_corrected / fields_extracted, 4)
        if fields_extracted
        else 0,
        "first_try_type_acc": round((len(confirms) - manual_changes) / len(confirms), 4)
        if confirms
        else 0,
        "median_latency_s": round(median_lat, 2),
        "p95_latency_s": round(p95_lat, 2),
        "corrections_by_category": by_cat,
    }
    return {"hours": hours, "buckets": bucket_list, "totals": totals}


# ----------------------- Runs (cross-run trace surface) --------------------
def _read_run_raws(raw_ref):
    """Load a raws/<id>.json trace by its event-recorded ref. Returns the parsed
    record or None (missing ref, missing/corrupt file — the seeded-state edge
    case). Best-effort: observability must never fail over IO."""
    if not raw_ref:
        return None
    path = os.path.join(BASE_DIR, str(raw_ref))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _stage_summary(calls):
    """Per-call stage labels for a run, in call order. Falls back to "call N"
    for any call written before stage labels existed (old raws)."""
    summary = []
    for i, c in enumerate(calls or []):
        stage = c.get("stage")
        summary.append(stage if stage else "call " + str(c.get("seq", i + 1)))
    return summary


@app.get("/runs")
async def get_runs(limit: int = 20):
    """Recent processed documents, newest first — the cross-run trace surface.

    One row per processed doc (extracted / unrecognized events), enriched from
    the raws/<id>.json trace when it is on disk. Seeded/older docs have no raws
    file: those rows report raw_available=false with a null call_count and empty
    stages, and still render. Model output itself is served per-doc by the
    existing GET /documents/<id>/trace; this endpoint is the index over it.
    """
    n = max(1, min(int(limit), 100))
    processed = [
        e for e in _read_events() if e.get("type") in ("extracted", "unrecognized")
    ]
    # Events are appended in processing order (chronological), so reverse gives
    # newest-first — the true order docs were handled, not a 1s-resolution ts sort.
    processed.reverse()

    runs = []
    for e in processed[:n]:
        raws = _read_run_raws(e.get("raw_ref"))
        if raws is not None:
            calls = raws.get("calls") or []
            model_runtime = raws.get("model_runtime")
            call_count = len(calls)
            stages = _stage_summary(calls)
            raw_available = True
        else:
            # No trace on disk (seeded/older doc). We don't know the call count
            # or per-call stages; the event still carries the run's headline.
            model_runtime = os.environ.get("MODEL_RUNTIME", "ollama")
            call_count = None
            stages = []
            raw_available = False
        runs.append({
            "doc_id": e.get("doc_id"),
            "doc_type": e.get("doc_type"),
            "status": e.get("type"),
            "model_runtime": model_runtime,
            "model_name": e.get("model"),
            "latency_s": e.get("latency_s"),
            "preprocessed": e.get("preprocessed"),
            "retried": bool(e.get("retried", False)),
            "call_count": call_count,
            "stages": stages,
            "raw_available": raw_available,
        })
    return {"runs": runs}


# --------------------- Static frontend (mount LAST) ------------------------
# API routes above win; the SPA + assets are served from "/" for everything else.
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/")
    async def _root():
        return JSONResponse({"service": "KeepBook", "frontend": "not found"})
